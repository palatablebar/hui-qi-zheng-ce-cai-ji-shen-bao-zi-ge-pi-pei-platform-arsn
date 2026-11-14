"""政策组控制器：组建组 / 调整名额与公示状态，全部留痕。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import allocation_service, audit_service


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def list_groups(db: Session) -> dict:
    return allocation_service.list_groups(db)


def get_group(db: Session, group_id: int) -> dict:
    return allocation_service.group_detail(db, allocation_service.get_group(db, group_id))


def create_group(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    group = allocation_service.create_group(db, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="group.create",
        entity="policy_group",
        entity_id=group.id,
        detail={
            "code": group.code,
            "name": group.name,
            "quota_official": group.quota_official,
            "quota_waitlist": group.quota_waitlist,
        },
        ip=_ip(request),
    )
    db.commit()
    return allocation_service.group_detail(db, group)


def update_group(db: Session, group_id: int, payload: dict, principal: dict, request: Request) -> dict:
    group = allocation_service.get_group(db, group_id)
    before = {
        "name": group.name,
        "quota_official": group.quota_official,
        "quota_waitlist": group.quota_waitlist,
        "public_status": group.public_status,
    }
    allocation_service.update_group(db, group, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="group.update",
        entity="policy_group",
        entity_id=group.id,
        detail={"code": group.code, "before": before, "fields": list(payload.keys())},
        ip=_ip(request),
    )
    db.commit()
    return allocation_service.group_detail(db, group)
