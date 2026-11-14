from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PolicyGroupIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=2, max_length=120)
    quota_official: int = Field(default=0, ge=0, le=100000)
    quota_waitlist: int = Field(default=0, ge=0, le=100000)
    public_status: Literal["unpublished", "publicizing", "finalized"] = "unpublished"
    note: str | None = Field(default=None, max_length=2000)


class PolicyGroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    quota_official: int | None = Field(default=None, ge=0, le=100000)
    quota_waitlist: int | None = Field(default=None, ge=0, le=100000)
    public_status: Literal["unpublished", "publicizing", "finalized"] | None = None
    note: str | None = Field(default=None, max_length=2000)
