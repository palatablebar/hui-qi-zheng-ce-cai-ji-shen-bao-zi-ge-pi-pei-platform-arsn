"""导出服务：申报清单 CSV（按优先级/得分排序）、名额台账 CSV 与政策台账 CSV。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CONFIDENCE_REVIEW_THRESHOLD,
    POLICY_STATUS_LABELS,
    PRIORITY_LABELS,
    PUBLIC_STATUS_LABELS,
    REVIEW_STATUS_LABELS,
    AllocationStatus,
)
from src.models.company import Company
from src.models.match import Match
from src.models.match_run import MatchRun
from src.models.policy import Policy
from src.models.policy_group import PolicyGroup
from src.models.rule import Rule
from src.services import allocation_service
from src.utils.csv_export import to_csv

PRIORITY_SORT = {"P0": 0, "P1": 1, "P2": 2}


def _queue_text(allocation: dict) -> str:
    if allocation["status"] == AllocationStatus.WAITLIST.value and allocation.get("queue_position"):
        return f"第 {allocation['queue_position']} 位"
    if allocation["status"] == AllocationStatus.OFFICIAL.value and allocation.get("promoted_from_id"):
        return "递补转正"
    if allocation["status"] == AllocationStatus.OFFICIAL.value and allocation.get("rank"):
        return f"位次 {allocation['rank']}"
    return ""


def matches_csv(db: Session, run_id: int | None = None) -> tuple[bytes, int]:
    stmt = select(Match)
    if run_id:
        stmt = stmt.where(Match.run_id == run_id)
    matches = list(db.scalars(stmt).all())
    policies = {item.id: item for item in db.scalars(select(Policy)).all()}
    companies = {item.id: item for item in db.scalars(select(Company)).all()}
    runs = {item.id: item for item in db.scalars(select(MatchRun)).all()}
    allocation_map = allocation_service.allocations_for_matches(db, matches)
    matches.sort(key=lambda item: (PRIORITY_SORT.get(item.priority, 9), -(item.score or 0), item.id))
    headers = [
        "序号", "运行编号", "政策编号", "政策标题", "发文机关", "申报截止",
        "企业名称", "所属行业", "注册地", "营业收入(万元)", "研发占比(%)",
        "员工人数", "匹配得分", "优先级", "硬条件", "缺口提示", "复核状态", "复核人", "复核备注",
        "政策组", "名额状态", "递补顺序", "递补关系",
    ]
    rows = []
    for index, item in enumerate(matches, start=1):
        policy = policies.get(item.policy_id)
        company = companies.get(item.company_id)
        run = runs.get(item.run_id)
        allocation = allocation_map.get((item.policy_id, item.company_id))
        rows.append(
            [
                index,
                run.run_no if run else "",
                policy.code if policy else "",
                policy.title if policy else "",
                policy.issuer if policy else "",
                policy.deadline.isoformat() if policy and policy.deadline else "",
                company.name if company else "",
                company.industry if company else "",
                company.district if company else "",
                f"{company.revenue_wan:g}" if company else "",
                f"{company.rd_ratio:g}" if company else "",
                company.employees if company else "",
                f"{item.score:.1f}",
                PRIORITY_LABELS.get(item.priority, item.priority),
                "通过" if item.hard_pass else "未通过",
                "；".join(item.gaps or []),
                REVIEW_STATUS_LABELS.get(item.status, item.status),
                item.reviewer or "",
                item.note or "",
                allocation["group_name"] if allocation else "",
                allocation["status_label"] if allocation else "",
                _queue_text(allocation) if allocation else "",
                "；".join(allocation["succession"]) if allocation else "",
            ]
        )
    return to_csv(headers, rows), len(matches)


def groups_csv(db: Session) -> tuple[bytes, int]:
    """名额台账：每组已占 / 剩余 / 互斥淘汰 / 递补关系，一行一条占位记录。"""
    groups = list(db.scalars(select(PolicyGroup).order_by(PolicyGroup.id)).all())
    headers = [
        "组编号", "组名称", "公示状态", "正式名额", "正式已占", "正式剩余",
        "候补名额", "候补已占", "互斥淘汰", "位次", "名额状态", "递补顺序",
        "企业名称", "政策编号", "政策标题", "匹配得分", "递补关系", "更新于",
    ]
    rows: list[list[object]] = []
    for group in groups:
        stats = allocation_service.group_stats(db, group.id)
        detail = allocation_service.group_detail(db, group)
        allocations = detail["allocations"]
        if not allocations:
            rows.append(
                [
                    group.code, group.name,
                    PUBLIC_STATUS_LABELS.get(group.public_status, group.public_status),
                    stats["official_quota"], stats["official_used"], stats["official_remaining"],
                    stats["waitlist_quota"], stats["waitlist_used"], stats["excluded_count"],
                    "", "", "", "", "", "", "", "", "",
                ]
            )
            continue
        score_by_pair = {
            (item.policy_id, item.company_id): item.score
            for item in db.scalars(
                select(Match).where(
                    Match.policy_id.in_([a["policy_id"] for a in allocations]),
                    Match.company_id.in_([a["company_id"] for a in allocations]),
                )
            ).all()
        }
        for allocation in allocations:
            score = score_by_pair.get((allocation["policy_id"], allocation["company_id"]))
            rows.append(
                [
                    group.code, group.name,
                    PUBLIC_STATUS_LABELS.get(group.public_status, group.public_status),
                    stats["official_quota"], stats["official_used"], stats["official_remaining"],
                    stats["waitlist_quota"], stats["waitlist_used"], stats["excluded_count"],
                    allocation["rank"] if allocation["rank"] is not None else "",
                    allocation["status_label"],
                    _queue_text(allocation),
                    allocation["company_name"] or "",
                    allocation["policy_code"] or "",
                    allocation["policy_title"] or "",
                    f"{score:.1f}" if score is not None else "",
                    "；".join(allocation["succession"]),
                    allocation["updated_at"] or "",
                ]
            )
    return to_csv(headers, rows), len(rows)


def policies_csv(db: Session) -> tuple[bytes, int]:
    policies = list(db.scalars(select(Policy).order_by(Policy.id)).all())
    rule_counts = {
        policy_id: count
        for policy_id, count in db.execute(select(Rule.policy_id, func.count()).group_by(Rule.policy_id)).all()
    }
    low_counts = {
        policy_id: count
        for policy_id, count in db.execute(
            select(Rule.policy_id, func.count())
            .where(Rule.confidence < CONFIDENCE_REVIEW_THRESHOLD)
            .group_by(Rule.policy_id)
        ).all()
    }
    headers = [
        "政策编号", "标题", "发文机关", "文号", "采集渠道", "状态",
        "申报截止", "资助上限(万元)", "条件数", "低置信条件数", "原文长度",
    ]
    rows = [
        [
            item.code,
            item.title,
            item.issuer,
            item.doc_no,
            item.source_name,
            POLICY_STATUS_LABELS.get(item.status, item.status),
            item.deadline.isoformat() if item.deadline else "",
            item.fund_cap_wan if item.fund_cap_wan is not None else "",
            rule_counts.get(item.id, 0),
            low_counts.get(item.id, 0),
            len(item.original_text or ""),
        ]
        for item in policies
    ]
    return to_csv(headers, rows), len(policies)
