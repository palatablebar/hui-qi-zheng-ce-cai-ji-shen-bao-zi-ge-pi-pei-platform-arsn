"""名额占位全链路（SQLite 内存库）：

一次匹配分组分配 → 重跑同一批幂等 → 撤销占位同组下一位递补 →
新批次只补剩余名额 → 公示锁定 → 工作台 / 导出同步展示。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.allocation import Allocation
from src.models.company import Company
from src.models.match import Match
from src.models.match_run import MatchRun
from src.models.policy import Policy
from src.models.policy_group import PolicyGroup
from src.services import allocation_service, export_service, match_service
from src.utils.errors import AppError

TODAY = date(2026, 11, 14)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _policy(db, code: str, group_id: int | None = None) -> Policy:
    policy = Policy(
        code=code,
        title=f"政策{code}",
        issuer="测试机关",
        deadline=date(2027, 1, 31),
        status="active",
        group_id=group_id,
    )
    db.add(policy)
    return policy


def _company(db, index: int) -> Company:
    company = Company(
        code=f"C{index:04d}",
        name=f"测试企业{index}号",
        industry="装备制造",
        district="园区",
        revenue_wan=1000.0,
        rd_ratio=5.0,
        employees=100,
        qualifications=[],
    )
    db.add(company)
    return company


def _bootstrap(db, quota_official=1, quota_waitlist=1, companies=4, public_status="unpublished"):
    group = PolicyGroup(
        code="GRP-T",
        name="测试政策组",
        quota_official=quota_official,
        quota_waitlist=quota_waitlist,
        public_status=public_status,
    )
    db.add(group)
    db.flush()
    p1 = _policy(db, "PT-1", group.id)
    p2 = _policy(db, "PT-2", group.id)
    for index in range(1, companies + 1):
        _company(db, index)
    db.flush()
    return group, p1, p2


def _run(db, codes=("PT-1", "PT-2")):
    return match_service.run_matching(
        db, operator_email="analyst@policyfit.io", policy_codes=list(codes), today=TODAY
    )


def _allocations(db, status=None):
    stmt = select(Allocation).order_by(Allocation.rank.is_(None), Allocation.rank, Allocation.id)
    if status:
        stmt = stmt.where(Allocation.status == status)
    return list(db.scalars(stmt).all())


def test_run_allocates_official_waitlist_and_exclusions(db):
    group, p1, p2 = _bootstrap(db)
    run = _run(db)
    assert run.metrics["allocations_created"] == 8
    assert db.scalar(select(func.count()).select_from(Match)) == 8  # 4 企业 × 2 政策全过硬条件
    official = _allocations(db, "official")
    waitlist = _allocations(db, "waitlist")
    excluded = _allocations(db, "excluded")
    assert len(official) == 1
    assert (official[0].company_id, official[0].policy_id, official[0].rank) == (1, p1.id, 1)
    assert len(waitlist) == 1
    assert (waitlist[0].company_id, waitlist[0].rank) == (2, 2)
    mutex = {(row.policy_id, row.company_id) for row in excluded if row.reason == "mutex"}
    quota = [row for row in excluded if row.reason == "quota"]
    assert mutex == {(p2.id, 1), (p2.id, 2)}
    assert len(quota) == 4
    stats = allocation_service.group_stats(db, group.id)
    assert stats["official_used"] == 1 and stats["official_remaining"] == 0
    assert stats["waitlist_used"] == 1 and stats["excluded_count"] == 2


def test_rerun_same_batch_adds_nothing_and_changes_nothing(db):
    _bootstrap(db)
    first = _run(db)
    snapshot = [
        (row.group_id, row.policy_id, row.company_id, row.status, row.rank)
        for row in _allocations(db)
    ]
    second = _run(db, codes=("PT-2", "PT-1"))  # 同一窗口，顺序不同也算同一批
    assert second.id == first.id
    assert getattr(second, "_reused") is True
    assert db.scalar(select(func.count()).select_from(MatchRun)) == 1
    assert db.scalar(select(func.count()).select_from(Match)) == 8
    assert db.scalar(select(func.count()).select_from(Allocation)) == 8
    after = [
        (row.group_id, row.policy_id, row.company_id, row.status, row.rank)
        for row in _allocations(db)
    ]
    assert after == snapshot


def test_revoke_official_promotes_next_waitlist_only(db):
    group, _, _ = _bootstrap(db)
    _run(db)
    official = _allocations(db, "official")[0]
    revoked, promoted = allocation_service.revoke(db, official, note="企业主动放弃")
    assert revoked.status == "revoked" and revoked.revoked_at is not None
    assert promoted is not None
    assert promoted.company_id == 2 and promoted.status == "official"
    assert promoted.promoted_from_id == revoked.id and promoted.promoted_at is not None
    detail = allocation_service.group_detail(db, group)
    promoted_item = next(item for item in detail["allocations"] if item["id"] == promoted.id)
    revoked_item = next(item for item in detail["allocations"] if item["id"] == revoked.id)
    assert any("递补" in text for text in promoted_item["succession"])
    assert any("递补" in text for text in revoked_item["succession"])
    # 再撤销同一条 → 409，不重复递补
    with pytest.raises(AppError):
        allocation_service.revoke(db, revoked)


def test_revoke_waitlist_promotes_nobody(db):
    _bootstrap(db)
    _run(db)
    waitlist = _allocations(db, "waitlist")[0]
    revoked, promoted = allocation_service.revoke(db, waitlist)
    assert revoked.status == "revoked"
    assert promoted is None


def test_revoke_without_waitlist_promotes_nobody(db):
    _bootstrap(db, quota_official=1, quota_waitlist=0, companies=2)
    _run(db)
    official = _allocations(db, "official")[0]
    _, promoted = allocation_service.revoke(db, official)
    assert promoted is None


def test_finalized_group_is_locked_for_runs_and_revokes(db):
    group, _, _ = _bootstrap(db)
    _run(db)
    allocation_service.update_group(db, group, {"public_status": "finalized"})
    official = _allocations(db, "official")[0]
    with pytest.raises(AppError):
        allocation_service.revoke(db, official)
    # 已公示为终态，不能回退
    with pytest.raises(AppError):
        allocation_service.update_group(db, group, {"public_status": "unpublished"})
    # 新批次运行也不向锁定组新增占位
    _policy(db, "PT-3", group.id)
    _company(db, 5)
    db.flush()
    before = db.scalar(select(func.count()).select_from(Allocation))
    _run(db, codes=("PT-1", "PT-2", "PT-3"))
    assert db.scalar(select(func.count()).select_from(Allocation)) == before


def test_new_batch_keeps_established_and_fills_remaining(db):
    group, p1, _ = _bootstrap(db, quota_official=2, quota_waitlist=0, companies=2)
    first = _run(db, codes=("PT-1",))
    officials = _allocations(db, "official")
    assert [row.company_id for row in officials] == [1, 2]
    allocation_service.revoke(db, officials[1])
    _company(db, 3)
    db.flush()
    second = _run(db, codes=("PT-1", "PT-2"))
    assert second.id != first.id
    c2 = db.scalar(select(Allocation).where(Allocation.company_id == 2))
    assert c2.status == "revoked"  # 既定结果不改换，撤销的企业不回炉
    c3 = db.scalar(select(Allocation).where(Allocation.company_id == 3))
    assert c3.status == "official" and c3.rank == 3 and c3.policy_id == p1.id
    c1 = db.scalar(select(Allocation).where(Allocation.company_id == 1))
    assert c1.status == "official" and c1.rank == 1


def test_match_listing_and_detail_carry_allocation(db):
    _bootstrap(db)
    run = _run(db)
    page = match_service.list_matches(db, run_id=run.id, limit=50)
    assert page["total"] == 8
    official_item = next(
        item for item in page["items"] if item["allocation"] and item["allocation"]["status"] == "official"
    )
    assert official_item["allocation"]["group_code"] == "GRP-T"
    wait_item = next(
        item for item in page["items"] if item["allocation"] and item["allocation"]["status"] == "waitlist"
    )
    assert wait_item["allocation"]["queue_position"] == 1
    detail = match_service.detail(db, match_service.get_match(db, official_item["id"]))
    assert detail["allocation"]["status_label"] == "正式占位"


def test_exports_show_group_stats_and_succession(db):
    _bootstrap(db)
    _run(db)
    content, count = export_service.matches_csv(db)
    assert count == 8
    text = content.decode("utf-8-sig")
    header = text.splitlines()[0]
    for column in ("政策组", "名额状态", "递补顺序", "递补关系"):
        assert column in header
    assert "正式占位" in text and "候补" in text and "互斥淘汰" in text
    official = _allocations(db, "official")[0]
    allocation_service.revoke(db, official)
    content, _ = export_service.matches_csv(db)
    text = content.decode("utf-8-sig")
    assert "递补「测试企业1号」撤销的占位" in text
    assert "已撤销" in text
    group_content, group_rows = export_service.groups_csv(db)
    group_text = group_content.decode("utf-8-sig")
    group_header = group_text.splitlines()[0]
    for column in ("正式已占", "正式剩余", "互斥淘汰", "递补关系"):
        assert column in group_header
    assert "测试政策组" in group_text and group_rows == 8
