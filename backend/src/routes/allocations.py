from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import ALLOCATION_REVOKE_ROLES
from src.controllers import allocation_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.allocation import AllocationRevokeIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/allocations", tags=["allocations"])


@router.get("")
def list_allocations(
    group_id: int | None = None,
    status: str | None = None,
    limit: int = Query(100),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return allocation_controller.list_allocations(
        db, group_id=group_id, status=status, limit=page.limit, offset=page.offset
    )


@router.post("/{allocation_id}/revoke")
def revoke(
    allocation_id: int,
    payload: AllocationRevokeIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ALLOCATION_REVOKE_ROLES)),
) -> dict:
    return allocation_controller.revoke(db, allocation_id, payload.model_dump(), principal, request)
