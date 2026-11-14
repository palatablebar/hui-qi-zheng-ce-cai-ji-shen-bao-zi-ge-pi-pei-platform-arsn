from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import GROUP_WRITE_ROLES
from src.controllers import policy_group_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.policy_group import PolicyGroupIn, PolicyGroupPatch

router = APIRouter(prefix="/policy-groups", tags=["policy-groups"])


@router.get("")
def list_groups(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return policy_group_controller.list_groups(db)


@router.get("/{group_id}")
def get_group(
    group_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return policy_group_controller.get_group(db, group_id)


@router.post("", status_code=201)
def create_group(
    payload: PolicyGroupIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*GROUP_WRITE_ROLES)),
) -> dict:
    return policy_group_controller.create_group(db, payload.model_dump(), principal, request)


@router.patch("/{group_id}")
def update_group(
    group_id: int,
    payload: PolicyGroupPatch,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*GROUP_WRITE_ROLES)),
) -> dict:
    return policy_group_controller.update_group(
        db, group_id, payload.model_dump(exclude_unset=True), principal, request
    )
