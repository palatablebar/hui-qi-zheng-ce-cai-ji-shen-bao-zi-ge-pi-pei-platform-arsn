"""匹配服务：执行资格匹配（算法 B）、名额分配、结果查询与复核。"""
from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    MATCH_RUN_STATUS_LABELS,
    PRIORITY_LABELS,
    REVIEW_STATUS_LABELS,
)
from src.models.company import Company
from src.models.match import Match
from src.models.match_run import MatchRun
from src.models.policy import Policy
from src.models.rule import Rule
from src.services import allocation_service, match_engine
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, local_now, utcnow


def batch_key_for(policy_codes: list[str]) -> str:
    """批次键：同一组政策窗口视为同一批；重跑同一批不新增名单、不改换既定结果。"""
    digest = hashlib.sha1(",".join(sorted(policy_codes)).encode("utf-8")).hexdigest()[:16]
    return f"batch-{digest}"


def next_run_no(db: Session) -> str:
    prefix = f"M{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(MatchRun).where(MatchRun.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:03d}"


def serialize_run(run: MatchRun) -> dict:
    return {
        "id": run.id,
        "run_no": run.run_no,
        "trigger": run.trigger,
        "status": run.status,
        "status_label": MATCH_RUN_STATUS_LABELS.get(run.status, run.status),
        "operator_email": run.operator_email,
        "policy_count": run.policy_count,
        "company_count": run.company_count,
        "match_count": run.match_count,
        "p0_count": run.p0_count,
        "params": run.params or {},
        "metrics": run.metrics or {},
        "log_text": run.log_text or "",
        "batch_key": run.batch_key,
        "reused": bool(getattr(run, "_reused", False)),
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def serialize_match(
    match: Match,
    *,
    policy: Policy | None = None,
    company: Company | None = None,
    run: MatchRun | None = None,
    allocation: dict | None = None,
) -> dict:
    return {
        "id": match.id,
        "run_id": match.run_id,
        "run_no": run.run_no if run else None,
        "policy_id": match.policy_id,
        "policy_code": policy.code if policy else None,
        "policy_title": policy.title if policy else None,
        "policy_deadline": iso_date(policy.deadline) if policy else None,
        "company_id": match.company_id,
        "company_name": company.name if company else None,
        "company_industry": company.industry if company else None,
        "company_district": company.district if company else None,
        "score": match.score,
        "hard_pass": match.hard_pass,
        "gaps": match.gaps or [],
        "priority": match.priority,
        "priority_label": PRIORITY_LABELS.get(match.priority, match.priority),
        "status": match.status,
        "status_label": REVIEW_STATUS_LABELS.get(match.status, match.status),
        "reviewer": match.reviewer,
        "note": match.note,
        "payload": match.payload or {},
        "allocation": allocation,
        "created_at": iso_utc(match.created_at),
        "updated_at": iso_utc(match.updated_at),
    }


def _load_maps(db: Session, matches: list[Match]) -> tuple[dict, dict, dict]:
    policy_ids = {item.policy_id for item in matches}
    company_ids = {item.company_id for item in matches}
    run_ids = {item.run_id for item in matches}
    policies = {
        row.id: row for row in db.scalars(select(Policy).where(Policy.id.in_(policy_ids))).all()
    } if policy_ids else {}
    companies = {
        row.id: row for row in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    } if company_ids else {}
    runs = {
        row.id: row for row in db.scalars(select(MatchRun).where(MatchRun.id.in_(run_ids))).all()
    } if run_ids else {}
    return policies, companies, runs


def run_matching(
    db: Session,
    *,
    operator_email: str,
    trigger: str = "manual",
    policy_codes: list[str] | None = None,
    today: date | None = None,
    seed_key: str | None = None,
) -> MatchRun:
    """执行一次匹配；同样的政策窗口 + 企业池两次结果一致（确定性）。

    批次幂等：政策窗口相同的运行共享 batch_key，重跑同一批直接返回既定运行，
    不新增名单、不改换既定结果（含名额占位与复核状态）。
    """
    if seed_key:
        existing = db.scalar(select(MatchRun).where(MatchRun.seed_key == seed_key))
        if existing is not None:
            existing._reused = True  # noqa: SLF001 - 瞬态标记，仅用于本次响应
            return existing

    if policy_codes:
        policies = list(db.scalars(select(Policy).where(Policy.code.in_(policy_codes))).all())
        found = {item.code for item in policies}
        missing = [code for code in policy_codes if code not in found]
        if missing:
            raise AppError("NOT_FOUND", 404, {"missing_policy_codes": missing})
    else:
        policies = list(db.scalars(select(Policy).where(Policy.status == "active")).all())

    if not policies:
        raise AppError("VALIDATION", 422, {"reason": "没有可匹配的政策（需为申报中）"})

    batch_key = batch_key_for([item.code for item in policies])
    existing = db.scalar(select(MatchRun).where(MatchRun.batch_key == batch_key))
    if existing is not None:
        existing._reused = True  # noqa: SLF001 - 瞬态标记，仅用于本次响应
        return existing

    companies = list(db.scalars(select(Company).order_by(Company.id)).all())
    reference = today or local_now().date()

    run = MatchRun(
        run_no=next_run_no(db),
        trigger=trigger,
        status="running",
        operator_email=operator_email,
        policy_count=len(policies),
        company_count=len(companies),
        params={"policy_codes": [item.code for item in policies], "today": reference.isoformat()},
        seed_key=seed_key,
        batch_key=batch_key,
    )
    db.add(run)
    db.flush()

    priority_counter = {"P0": 0, "P1": 0, "P2": 0}
    scores: list[float] = []
    match_count = 0
    created_matches: list[Match] = []
    log_lines: list[str] = [f"匹配窗口：{len(policies)} 条政策 × {len(companies)} 家企业"]
    for policy in policies:
        rules = list(db.scalars(select(Rule).where(Rule.policy_id == policy.id).order_by(Rule.id)).all())
        rule_dicts = [
            {
                "field": item.field,
                "op": item.op,
                "value": item.value,
                "unit": item.unit,
                "required": item.required,
                "confidence": item.confidence,
                "evidence_sentence": item.evidence_sentence,
            }
            for item in rules
        ]
        policy_ctx = {
            "code": policy.code,
            "title": policy.title,
            "deadline": policy.deadline.isoformat() if policy.deadline else None,
        }
        passed = 0
        for company in companies:
            company_ctx = {
                "code": company.code,
                "industry": company.industry,
                "district": company.district,
                "revenue_wan": company.revenue_wan,
                "rd_ratio": company.rd_ratio,
                "employees": company.employees,
                "qualifications": company.qualifications or [],
            }
            result = match_engine.evaluate(company_ctx, rule_dicts, policy_ctx, reference)
            if not result["hard_pass"]:
                continue
            passed += 1
            match_count += 1
            priority_counter[result["priority"]] += 1
            scores.append(result["score"])
            match = Match(
                run_id=run.id,
                policy_id=policy.id,
                company_id=company.id,
                score=result["score"],
                hard_pass=True,
                gaps=result["gaps"],
                priority=result["priority"],
                status="pending",
                payload=result["payload"],
            )
            db.add(match)
            created_matches.append(match)
        log_lines.append(
            f"{policy.code} {policy.title}：条件 {len(rule_dicts)} 条，硬条件通过 {passed} 家"
        )
    db.flush()

    allocation_result = allocation_service.allocate_for_run(
        db, run_id=run.id, batch_key=batch_key, policies=policies, matches=created_matches
    )
    for summary in allocation_result["groups"]:
        if summary["locked"]:
            log_lines.append(
                f"{summary['code']} {summary['name']}：已公示锁定，既定名单不动"
            )
        else:
            log_lines.append(
                f"{summary['code']} {summary['name']}：正式 {summary['official_used']}/{summary['official_quota']}"
                f"（余 {summary['official_remaining']}），候补 {summary['waitlist_used']}/{summary['waitlist_quota']}，"
                f"互斥淘汰 {summary['excluded_count']}，本批新增占位 {summary['created']}"
            )

    run.match_count = match_count
    run.p0_count = priority_counter["P0"]
    run.status = "success" if match_count else "partial"
    run.metrics = {
        "policies": len(policies),
        "companies": len(companies),
        "pairs": len(policies) * len(companies),
        "hard_pass": match_count,
        "p0": priority_counter["P0"],
        "p1": priority_counter["P1"],
        "p2": priority_counter["P2"],
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "top_score": max(scores) if scores else 0.0,
        "today": reference.isoformat(),
        "allocations_created": allocation_result["created"],
        "groups": allocation_result["groups"],
    }
    run.log_text = "\n".join(log_lines + [f"合计进入申报清单 {match_count} 条，P0 {priority_counter['P0']} 条"])
    run.finished_at = utcnow()
    db.flush()
    return run


def list_runs(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(MatchRun)
    count_stmt = select(func.count()).select_from(MatchRun)
    if status:
        stmt = stmt.where(MatchRun.status == status)
        count_stmt = count_stmt.where(MatchRun.status == status)
    total = db.scalar(count_stmt) or 0
    rows = list(db.scalars(stmt.order_by(MatchRun.id.desc()).limit(limit).offset(offset)).all())
    return {"items": [serialize_run(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_run(db: Session, run_id: int) -> MatchRun:
    run = db.get(MatchRun, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "match_run", "id": run_id})
    return run


def list_matches(
    db: Session,
    *,
    run_id: int | None = None,
    policy_id: int | None = None,
    company_id: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(Match)
    count_stmt = select(func.count()).select_from(Match)
    if run_id:
        stmt = stmt.where(Match.run_id == run_id)
        count_stmt = count_stmt.where(Match.run_id == run_id)
    if policy_id:
        stmt = stmt.where(Match.policy_id == policy_id)
        count_stmt = count_stmt.where(Match.policy_id == policy_id)
    if company_id:
        stmt = stmt.where(Match.company_id == company_id)
        count_stmt = count_stmt.where(Match.company_id == company_id)
    if priority:
        stmt = stmt.where(Match.priority == priority)
        count_stmt = count_stmt.where(Match.priority == priority)
    if status:
        stmt = stmt.where(Match.status == status)
        count_stmt = count_stmt.where(Match.status == status)
    if q:
        like = f"%{q.strip()}%"
        company_ids = [
            row for (row,) in db.execute(select(Company.id).where(Company.name.ilike(like))).all()
        ]
        policy_ids = [
            row for (row,) in db.execute(select(Policy.id).where(or_(Policy.title.ilike(like), Policy.code.ilike(like)))).all()
        ]
        clause = or_(
            Match.company_id.in_(company_ids or [-1]),
            Match.policy_id.in_(policy_ids or [-1]),
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    rows = list(
        db.scalars(
            stmt.order_by(Match.score.desc(), Match.id).limit(limit).offset(offset)
        ).all()
    )
    policies, companies, runs = _load_maps(db, rows)
    allocation_map = allocation_service.allocations_for_matches(db, rows)
    items = [
        serialize_match(
            row,
            policy=policies.get(row.policy_id),
            company=companies.get(row.company_id),
            run=runs.get(row.run_id),
            allocation=allocation_map.get((row.policy_id, row.company_id)),
        )
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_match(db: Session, match_id: int) -> Match:
    match = db.get(Match, match_id)
    if match is None:
        raise AppError("NOT_FOUND", 404, {"entity": "match", "id": match_id})
    return match


def detail(db: Session, match: Match) -> dict:
    policy = db.get(Policy, match.policy_id)
    company = db.get(Company, match.company_id)
    run = db.get(MatchRun, match.run_id)
    allocation_map = allocation_service.allocations_for_matches(db, [match])
    data = serialize_match(
        match,
        policy=policy,
        company=company,
        run=run,
        allocation=allocation_map.get((match.policy_id, match.company_id)),
    )
    data["company"] = (
        {
            "id": company.id,
            "code": company.code,
            "name": company.name,
            "industry": company.industry,
            "district": company.district,
            "revenue_wan": company.revenue_wan,
            "rd_ratio": company.rd_ratio,
            "employees": company.employees,
            "qualifications": company.qualifications or [],
            "contact": company.contact,
            "owner": company.owner,
        }
        if company
        else None
    )
    data["policy"] = (
        {
            "id": policy.id,
            "code": policy.code,
            "title": policy.title,
            "issuer": policy.issuer,
            "deadline": iso_date(policy.deadline),
            "fund_cap_wan": policy.fund_cap_wan,
        }
        if policy
        else None
    )
    return data


def review(db: Session, match: Match, *, status: str, note: str | None, reviewer: str) -> Match:
    match.status = status
    match.note = note
    match.reviewer = reviewer
    match.updated_at = utcnow()
    db.flush()
    return match
