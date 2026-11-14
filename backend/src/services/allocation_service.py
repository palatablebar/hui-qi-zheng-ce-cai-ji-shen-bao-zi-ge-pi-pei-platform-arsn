"""名额服务：政策组 CRUD、一次运行的名额分配、撤销与递补、组统计。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    ALLOCATION_STATUS_LABELS,
    EXCLUSION_REASON_LABELS,
    PUBLIC_STATUS_LABELS,
    PUBLIC_STATUS_TRANSITIONS,
    AllocationStatus,
    ExclusionReason,
    PublicStatus,
    allocation_status_text,
)
from src.models.allocation import Allocation
from src.models.company import Company
from src.models.match import Match
from src.models.policy import Policy
from src.models.policy_group import PolicyGroup
from src.services import allocation_engine
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc, utcnow

ACTIVE_STATUSES = (AllocationStatus.OFFICIAL.value, AllocationStatus.WAITLIST.value)


# ---------------------------------------------------------------- 序列化
def group_stats(db: Session, group_id: int) -> dict:
    rows = list(db.scalars(select(Allocation).where(Allocation.group_id == group_id)).all())
    official_used = sum(1 for row in rows if row.status == AllocationStatus.OFFICIAL.value)
    waitlist_used = sum(1 for row in rows if row.status == AllocationStatus.WAITLIST.value)
    excluded_mutex = sum(
        1
        for row in rows
        if row.status == AllocationStatus.EXCLUDED.value and row.reason == ExclusionReason.MUTEX.value
    )
    excluded_quota = sum(
        1
        for row in rows
        if row.status == AllocationStatus.EXCLUDED.value and row.reason == ExclusionReason.QUOTA.value
    )
    revoked = sum(1 for row in rows if row.status == AllocationStatus.REVOKED.value)
    group = db.get(PolicyGroup, group_id)
    quota_official = group.quota_official if group else 0
    quota_waitlist = group.quota_waitlist if group else 0
    return {
        "official_quota": quota_official,
        "official_used": official_used,
        "official_remaining": max(0, quota_official - official_used),
        "waitlist_quota": quota_waitlist,
        "waitlist_used": waitlist_used,
        "waitlist_remaining": max(0, quota_waitlist - waitlist_used),
        "excluded_count": excluded_mutex,
        "excluded_quota_count": excluded_quota,
        "revoked_count": revoked,
    }


def serialize_group(db: Session, group: PolicyGroup, *, with_stats: bool = True) -> dict:
    data = {
        "id": group.id,
        "code": group.code,
        "name": group.name,
        "quota_official": group.quota_official,
        "quota_waitlist": group.quota_waitlist,
        "public_status": group.public_status,
        "public_status_label": PUBLIC_STATUS_LABELS.get(group.public_status, group.public_status),
        "locked": group.public_status == PublicStatus.FINALIZED.value,
        "note": group.note or "",
        "created_at": iso_utc(group.created_at),
        "updated_at": iso_utc(group.updated_at),
    }
    if with_stats:
        data["stats"] = group_stats(db, group.id)
    return data


def _queue_positions(rows: list[Allocation]) -> dict[int, int]:
    """候补水位：按位次排序后的当前递补顺序（1 起），只计仍在候补的记录。"""
    waiting = sorted(
        (row for row in rows if row.status == AllocationStatus.WAITLIST.value),
        key=lambda row: (row.rank or 0, row.id),
    )
    return {row.id: index for index, row in enumerate(waiting, start=1)}


def serialize_allocation(
    row: Allocation,
    *,
    group: PolicyGroup | None = None,
    policy: Policy | None = None,
    company: Company | None = None,
    promoted_from: Allocation | None = None,
    promoted_from_company: Company | None = None,
    succeeded_by_company: Company | None = None,
    queue_position: int | None = None,
) -> dict:
    succession: list[str] = []
    if promoted_from is not None:
        name = promoted_from_company.name if promoted_from_company else f"#{row.promoted_from_id}"
        succession.append(f"递补「{name}」撤销的占位")
    if succeeded_by_company is not None:
        succession.append(f"由「{succeeded_by_company.name}」递补")
    return {
        "id": row.id,
        "group_id": row.group_id,
        "group_code": group.code if group else None,
        "group_name": group.name if group else None,
        "public_status": group.public_status if group else None,
        "policy_id": row.policy_id,
        "policy_code": policy.code if policy else None,
        "policy_title": policy.title if policy else None,
        "company_id": row.company_id,
        "company_code": company.code if company else None,
        "company_name": company.name if company else None,
        "status": row.status,
        "status_label": allocation_status_text(row.status, row.reason),
        "reason": row.reason,
        "reason_label": EXCLUSION_REASON_LABELS.get(row.reason or "", row.reason),
        "rank": row.rank,
        "queue_position": queue_position,
        "run_id": row.run_id,
        "batch_key": row.batch_key,
        "promoted_from_id": row.promoted_from_id,
        "succession": succession,
        "note": row.note,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
        "revoked_at": iso_utc(row.revoked_at),
        "promoted_at": iso_utc(row.promoted_at),
    }


def _serialize_allocations(db: Session, rows: list[Allocation]) -> list[dict]:
    if not rows:
        return []
    group_ids = {row.group_id for row in rows}
    policy_ids = {row.policy_id for row in rows}
    company_ids = {row.company_id for row in rows}
    promoted_ids = {row.promoted_from_id for row in rows if row.promoted_from_id}
    row_ids = {row.id for row in rows}
    groups = {
        item.id: item
        for item in db.scalars(select(PolicyGroup).where(PolicyGroup.id.in_(group_ids))).all()
    }
    policies = {
        item.id: item for item in db.scalars(select(Policy).where(Policy.id.in_(policy_ids))).all()
    }
    companies = {
        item.id: item for item in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    }
    promoted_rows = {
        item.id: item
        for item in db.scalars(select(Allocation).where(Allocation.id.in_(promoted_ids))).all()
    } if promoted_ids else {}
    successor_rows = list(
        db.scalars(select(Allocation).where(Allocation.promoted_from_id.in_(row_ids))).all()
    )
    successors = {row.promoted_from_id: row for row in successor_rows}
    extra_company_ids = {
        *(row.company_id for row in promoted_rows.values()),
        *(row.company_id for row in successor_rows),
    } - companies.keys()
    if extra_company_ids:
        companies.update(
            {
                item.id: item
                for item in db.scalars(select(Company).where(Company.id.in_(extra_company_ids))).all()
            }
        )
    wait_rows = list(
        db.scalars(
            select(Allocation).where(
                Allocation.group_id.in_(group_ids),
                Allocation.status == AllocationStatus.WAITLIST.value,
            )
        ).all()
    )
    positions = _queue_positions(wait_rows)
    items = []
    for row in rows:
        promoted_from = promoted_rows.get(row.promoted_from_id) if row.promoted_from_id else None
        successor = successors.get(row.id)
        items.append(
            serialize_allocation(
                row,
                group=groups.get(row.group_id),
                policy=policies.get(row.policy_id),
                company=companies.get(row.company_id),
                promoted_from=promoted_from,
                promoted_from_company=(
                    companies.get(promoted_from.company_id) if promoted_from else None
                ),
                succeeded_by_company=(
                    companies.get(successor.company_id) if successor else None
                ),
                queue_position=positions.get(row.id),
            )
        )
    return items


# ---------------------------------------------------------------- 政策组 CRUD
def list_groups(db: Session) -> dict:
    groups = list(db.scalars(select(PolicyGroup).order_by(PolicyGroup.id)).all())
    policy_rows = list(
        db.scalars(
            select(Policy).where(Policy.group_id.is_not(None)).order_by(Policy.id)
        ).all()
    )
    policies_by_group: dict[int, list[Policy]] = {}
    for policy in policy_rows:
        policies_by_group.setdefault(policy.group_id, []).append(policy)
    items = []
    for group in groups:
        item = serialize_group(db, group)
        item["policies"] = [
            {
                "id": policy.id,
                "code": policy.code,
                "title": policy.title,
                "deadline": iso_date(policy.deadline),
            }
            for policy in policies_by_group.get(group.id, [])
        ]
        items.append(item)
    return {"items": items, "total": len(items)}


def get_group(db: Session, group_id: int) -> PolicyGroup:
    group = db.get(PolicyGroup, group_id)
    if group is None:
        raise not_found("policy_group", group_id)
    return group


def group_detail(db: Session, group: PolicyGroup) -> dict:
    data = serialize_group(db, group)
    policies = list(
        db.scalars(select(Policy).where(Policy.group_id == group.id).order_by(Policy.id)).all()
    )
    data["policies"] = [
        {"id": item.id, "code": item.code, "title": item.title, "deadline": iso_date(item.deadline)}
        for item in policies
    ]
    rows = list(
        db.scalars(
            select(Allocation)
            .where(Allocation.group_id == group.id)
            .order_by(Allocation.rank.is_(None), Allocation.rank, Allocation.id)
        ).all()
    )
    data["allocations"] = _serialize_allocations(db, rows)
    return data


def create_group(db: Session, payload: dict) -> PolicyGroup:
    exists = db.scalar(select(PolicyGroup).where(PolicyGroup.code == payload["code"]))
    if exists is not None:
        raise AppError("CONFLICT", 409, {"code": payload["code"]})
    group = PolicyGroup(
        code=payload["code"],
        name=payload["name"],
        quota_official=payload.get("quota_official") or 0,
        quota_waitlist=payload.get("quota_waitlist") or 0,
        public_status=payload.get("public_status") or PublicStatus.UNPUBLISHED.value,
        note=payload.get("note") or "",
    )
    db.add(group)
    db.flush()
    return group


def update_group(db: Session, group: PolicyGroup, payload: dict) -> PolicyGroup:
    for field in ("name", "note"):
        if field in payload and payload[field] is not None:
            setattr(group, field, payload[field])
    for field in ("quota_official", "quota_waitlist"):
        if field in payload and payload[field] is not None:
            setattr(group, field, int(payload[field]))
    if payload.get("public_status") and payload["public_status"] != group.public_status:
        target = payload["public_status"]
        allowed = PUBLIC_STATUS_TRANSITIONS.get(group.public_status, set())
        if target not in allowed:
            raise AppError(
                "CONFLICT",
                409,
                {"from": group.public_status, "to": target, "reason": "已公示的名单为终态，不能回退"},
            )
        group.public_status = target
    db.flush()
    return group


# ---------------------------------------------------------------- 运行分配
def allocate_for_run(
    db: Session,
    *,
    run_id: int,
    batch_key: str,
    policies: list[Policy],
    matches: list[Match],
) -> dict:
    """把本次运行硬条件全过的候选按组分配名额；已公示（finalized）的组锁定不动。"""
    group_ids = sorted({policy.group_id for policy in policies if policy.group_id})
    if not group_ids:
        return {"created": 0, "groups": []}
    groups = {
        item.id: item
        for item in db.scalars(select(PolicyGroup).where(PolicyGroup.id.in_(group_ids))).all()
    }
    created = 0
    summaries: list[dict] = []
    for group_id in group_ids:
        group = groups.get(group_id)
        if group is None:
            continue
        group_policies = [item for item in policies if item.group_id == group_id]
        if group.public_status == PublicStatus.FINALIZED.value:
            summaries.append(
                {
                    "group_id": group.id,
                    "code": group.code,
                    "name": group.name,
                    "public_status": group.public_status,
                    "locked": True,
                    "created": 0,
                    **group_stats(db, group.id),
                }
            )
            continue
        existing_rows = list(
            db.scalars(select(Allocation).where(Allocation.group_id == group.id)).all()
        )
        existing = [
            {"company_id": row.company_id, "status": row.status, "rank": row.rank}
            for row in existing_rows
        ]
        group_policy_ids = {policy.id for policy in group_policies}
        candidates = [
            {
                "policy_id": match.policy_id,
                "company_id": match.company_id,
                "score": match.score,
                "match_id": match.id,
            }
            for match in matches
            if match.policy_id in group_policy_ids
        ]
        decisions = allocation_engine.allocate(
            candidates, existing, group.quota_official, group.quota_waitlist
        )
        for decision in decisions:
            db.add(
                Allocation(
                    group_id=group.id,
                    policy_id=decision["policy_id"],
                    company_id=decision["company_id"],
                    run_id=run_id,
                    match_id=decision.get("match_id"),
                    batch_key=batch_key,
                    status=decision["status"],
                    reason=decision["reason"],
                    rank=decision["rank"],
                )
            )
            created += 1
        db.flush()
        summaries.append(
            {
                "group_id": group.id,
                "code": group.code,
                "name": group.name,
                "public_status": group.public_status,
                "locked": False,
                "created": len(decisions),
                **group_stats(db, group.id),
            }
        )
    return {"created": created, "groups": summaries}


# ---------------------------------------------------------------- 撤销与递补
def revoke(db: Session, allocation: Allocation, *, note: str | None = None) -> tuple[Allocation, Allocation | None]:
    """撤销一个占位：正式占位被撤销时，同组下一位候补递补转正（只递补一位）。"""
    group = db.get(PolicyGroup, allocation.group_id)
    if group is not None and group.public_status == PublicStatus.FINALIZED.value:
        raise AppError("CONFLICT", 409, {"group": group.code, "reason": "名单已公示，占位不可撤销"})
    if allocation.status not in ACTIVE_STATUSES:
        raise AppError(
            "CONFLICT",
            409,
            {"status": allocation.status, "reason": "只有正式或候补占位可以撤销"},
        )
    was_official = allocation.status == AllocationStatus.OFFICIAL.value
    allocation.status = AllocationStatus.REVOKED.value
    allocation.revoked_at = utcnow()
    if note:
        allocation.note = note
    promoted: Allocation | None = None
    if was_official:
        promoted = db.scalar(
            select(Allocation)
            .where(
                Allocation.group_id == allocation.group_id,
                Allocation.status == AllocationStatus.WAITLIST.value,
            )
            .order_by(Allocation.rank, Allocation.id)
            .limit(1)
        )
        if promoted is not None:
            promoted.status = AllocationStatus.OFFICIAL.value
            promoted.promoted_from_id = allocation.id
            promoted.promoted_at = utcnow()
    db.flush()
    return allocation, promoted


def get_allocation(db: Session, allocation_id: int) -> Allocation:
    allocation = db.get(Allocation, allocation_id)
    if allocation is None:
        raise not_found("allocation", allocation_id)
    return allocation


def allocation_detail(db: Session, allocation: Allocation) -> dict:
    return _serialize_allocations(db, [allocation])[0]


def list_allocations(
    db: Session,
    *,
    group_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(Allocation)
    count_stmt = select(func.count()).select_from(Allocation)
    if group_id:
        stmt = stmt.where(Allocation.group_id == group_id)
        count_stmt = count_stmt.where(Allocation.group_id == group_id)
    if status:
        stmt = stmt.where(Allocation.status == status)
        count_stmt = count_stmt.where(Allocation.status == status)
    total = db.scalar(count_stmt) or 0
    rows = list(
        db.scalars(
            stmt.order_by(Allocation.rank.is_(None), Allocation.rank, Allocation.id)
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return {
        "items": _serialize_allocations(db, rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def allocations_for_matches(db: Session, matches: list[Match]) -> dict[tuple[int, int], dict]:
    """按 (policy_id, company_id) 汇总名额信息，供匹配列表 / 详情同步展示。"""
    pairs = {(item.policy_id, item.company_id) for item in matches}
    if not pairs:
        return {}
    policy_ids = {pair[0] for pair in pairs}
    company_ids = {pair[1] for pair in pairs}
    rows = list(
        db.scalars(
            select(Allocation).where(
                Allocation.policy_id.in_(policy_ids),
                Allocation.company_id.in_(company_ids),
            )
        ).all()
    )
    serialized = _serialize_allocations(db, rows)
    return {
        (row.policy_id, row.company_id): item
        for row, item in zip(rows, serialized, strict=True)
    }
