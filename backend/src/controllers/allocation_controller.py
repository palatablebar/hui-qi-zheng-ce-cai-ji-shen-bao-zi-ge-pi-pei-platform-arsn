"""名额控制器：占位查询、撤销与递补（撤销只让同组下一位递补）。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import allocation_service, audit_service


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def list_allocations(db: Session, **filters) -> dict:
    return allocation_service.list_allocations(db, **filters)


def revoke(db: Session, allocation_id: int, payload: dict, principal: dict, request: Request) -> dict:
    allocation = allocation_service.get_allocation(db, allocation_id)
    revoked, promoted = allocation_service.revoke(db, allocation, note=payload.get("note"))
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="allocation.revoke",
        entity="allocation",
        entity_id=revoked.id,
        detail={
            "group_id": revoked.group_id,
            "company_id": revoked.company_id,
            "policy_id": revoked.policy_id,
            "note": payload.get("note"),
        },
        ip=_ip(request),
    )
    if promoted is not None:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="allocation.promote",
            entity="allocation",
            entity_id=promoted.id,
            detail={
                "group_id": promoted.group_id,
                "company_id": promoted.company_id,
                "policy_id": promoted.policy_id,
                "promoted_from_id": revoked.id,
            },
            ip=_ip(request),
        )
    db.commit()
    return {
        "revoked": allocation_service.allocation_detail(db, revoked),
        "promoted": allocation_service.allocation_detail(db, promoted) if promoted else None,
        "group": allocation_service.group_detail(
            db, allocation_service.get_group(db, revoked.group_id)
        ),
    }
