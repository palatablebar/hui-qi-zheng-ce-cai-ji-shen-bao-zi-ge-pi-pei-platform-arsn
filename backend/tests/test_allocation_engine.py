"""名额分配引擎（纯函数）单测：统一顺序、互斥淘汰、名额上限、既定位次续排、确定性。"""
from __future__ import annotations

from src.services.allocation_engine import allocate


def cand(policy_id: int, company_id: int, score: float) -> dict:
    return {"policy_id": policy_id, "company_id": company_id, "score": score, "match_id": None}


def test_unified_order_by_score_desc():
    decisions = allocate(
        [cand(1, 1, 80.0), cand(1, 2, 90.0), cand(1, 3, 70.0)],
        [],
        quota_official=2,
        quota_waitlist=1,
    )
    by_company = {item["company_id"]: item for item in decisions}
    assert by_company[2]["status"] == "official" and by_company[2]["rank"] == 1
    assert by_company[1]["status"] == "official" and by_company[1]["rank"] == 2
    assert by_company[3]["status"] == "waitlist" and by_company[3]["rank"] == 3


def test_score_tie_breaks_by_company_id_then_policy_id():
    decisions = allocate(
        [cand(2, 9, 88.0), cand(1, 9, 88.0), cand(1, 3, 88.0)],
        [],
        quota_official=1,
        quota_waitlist=1,
    )
    assert decisions[0]["company_id"] == 3 and decisions[0]["status"] == "official"
    assert decisions[1]["company_id"] == 9 and decisions[1]["policy_id"] == 1
    assert decisions[1]["status"] == "waitlist"
    assert decisions[2]["status"] == "excluded" and decisions[2]["reason"] == "mutex"


def test_mutex_one_slot_per_company_per_group():
    decisions = allocate(
        [cand(1, 1, 95.0), cand(2, 1, 90.0), cand(2, 2, 85.0)],
        [],
        quota_official=2,
        quota_waitlist=0,
    )
    by_pair = {(item["policy_id"], item["company_id"]): item for item in decisions}
    assert by_pair[(1, 1)]["status"] == "official"
    assert by_pair[(2, 1)]["status"] == "excluded" and by_pair[(2, 1)]["reason"] == "mutex"
    assert by_pair[(2, 2)]["status"] == "official"


def test_quota_full_marks_quota_exclusion():
    decisions = allocate(
        [cand(1, 1, 90.0), cand(1, 2, 80.0), cand(1, 3, 70.0)],
        [],
        quota_official=1,
        quota_waitlist=1,
    )
    assert decisions[2]["company_id"] == 3
    assert decisions[2]["status"] == "excluded" and decisions[2]["reason"] == "quota"
    assert decisions[2]["rank"] is None


def test_existing_allocations_are_kept_and_ranks_continue():
    existing = [
        {"company_id": 1, "status": "official", "rank": 1},
        {"company_id": 2, "status": "waitlist", "rank": 2},
        {"company_id": 3, "status": "revoked", "rank": None},
        {"company_id": 4, "status": "excluded", "rank": None},
    ]
    decisions = allocate(
        [cand(1, 1, 99.0), cand(1, 3, 99.0), cand(1, 4, 99.0), cand(1, 5, 60.0), cand(1, 6, 59.0)],
        existing,
        quota_official=2,
        quota_waitlist=2,
    )
    by_company = {item["company_id"]: item for item in decisions}
    # 既有占位（含已撤销/已淘汰）的企业不再参与：1/3/4 都不会有新决定
    assert set(by_company) == {5, 6}
    assert by_company[5]["status"] == "official" and by_company[5]["rank"] == 3
    assert by_company[6]["status"] == "waitlist" and by_company[6]["rank"] == 4


def test_existing_fills_quota_then_new_candidates_excluded():
    existing = [
        {"company_id": 1, "status": "official", "rank": 1},
        {"company_id": 2, "status": "waitlist", "rank": 2},
    ]
    decisions = allocate([cand(1, 9, 100.0)], existing, quota_official=1, quota_waitlist=1)
    assert decisions[0]["status"] == "excluded" and decisions[0]["reason"] == "quota"


def test_allocate_is_deterministic():
    candidates = [cand(1, 1, 90.0), cand(2, 1, 88.0), cand(1, 2, 85.0), cand(2, 3, 70.0)]
    first = allocate(candidates, [], 1, 1)
    second = allocate(list(candidates), [], 1, 1)
    assert first == second
