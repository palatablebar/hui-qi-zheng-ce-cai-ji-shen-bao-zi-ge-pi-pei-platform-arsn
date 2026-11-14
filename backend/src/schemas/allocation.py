from __future__ import annotations

from pydantic import BaseModel, Field


class AllocationRevokeIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
