"""政策组：互斥政策的集合 + 组级名额（正式 / 候补）+ 公示状态。

同组政策互斥：同一企业在同组最多占一个名额；名额按组核算，不按项目分别核算。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class PolicyGroup(Base):
    __tablename__ = "policy_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    quota_official: Mapped[int] = mapped_column(Integer, default=0)
    quota_waitlist: Mapped[int] = mapped_column(Integer, default=0)
    public_status: Mapped[str] = mapped_column(String(16), default="unpublished", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
