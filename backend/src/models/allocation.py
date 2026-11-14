"""名额占位台账：政策组 × 企业 的一次占位决定（正式 / 候补 / 淘汰 / 撤销）。

占位是跨运行持久的：同一 (group, policy, company) 只有一条记录；
重跑同一批不新增、不改换；撤销正式占位时由同组下一位候补递补（promoted_from_id 留痕）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (
        UniqueConstraint("group_id", "policy_id", "company_id", name="uq_allocations_group_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("policy_groups.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("match_runs.id", ondelete="CASCADE"), index=True
    )
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id", ondelete="SET NULL"), nullable=True
    )
    batch_key: Mapped[str] = mapped_column(String(60), index=True, default="")
    status: Mapped[str] = mapped_column(String(16), default="waitlist", index=True)
    reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promoted_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocations.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
