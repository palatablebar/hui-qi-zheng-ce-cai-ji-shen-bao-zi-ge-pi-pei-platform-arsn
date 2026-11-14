"""名额分配（纯函数）：政策组名额 × 互斥关系 → 正式 / 候补 / 淘汰。

不访问数据库、不访问网络；同输入两次结果逐字一致（确定性）。

口径（README §10 同步）：
  候选：一次运行中「硬条件全过」的 (policy, company, score) 组合。
  统一顺序：得分降序 → 企业 id 升序 → 政策 id 升序。
  互斥：同一企业在同组最多占一个名额，落在其得分最高的政策；
        该企业在组内其余组合记「互斥淘汰」。
  名额：先分正式名额，再分候补名额；都满记「名额已满」淘汰。
  既定结果不改：组内已有占位记录（含已撤销 / 已淘汰）的企业不再参与新一轮分配；
        新分配的位次从既定位次最大值之后续排，既定位次不变。
"""
from __future__ import annotations

from src.constants.enums import AllocationStatus, ExclusionReason

OFFICIAL = AllocationStatus.OFFICIAL.value
WAITLIST = AllocationStatus.WAITLIST.value
EXCLUDED = AllocationStatus.EXCLUDED.value
REASON_MUTEX = ExclusionReason.MUTEX.value
REASON_QUOTA = ExclusionReason.QUOTA.value


def _sort_key(candidate: dict) -> tuple:
    return (
        -float(candidate.get("score") or 0.0),
        int(candidate["company_id"]),
        int(candidate["policy_id"]),
    )


def allocate(
    candidates: list[dict],
    existing: list[dict],
    quota_official: int,
    quota_waitlist: int,
) -> list[dict]:
    """为一组候选分配名额。

    candidates：本次运行硬条件全过的 {policy_id, company_id, score, ...}。
    existing：组内既有占位记录 {company_id, status, rank}（任意状态都算既定结果）。
    返回：每条候选一个决定 {..., status, reason, rank}；顺序与统一顺序一致。
    """
    decided = {int(item["company_id"]) for item in existing}
    official_used = sum(1 for item in existing if item["status"] == OFFICIAL)
    waitlist_used = sum(1 for item in existing if item["status"] == WAITLIST)
    remaining_official = max(0, int(quota_official) - official_used)
    remaining_waitlist = max(0, int(quota_waitlist) - waitlist_used)
    next_rank = max([int(item["rank"] or 0) for item in existing], default=0) + 1

    decisions: list[dict] = []
    slotted: set[int] = set()
    fresh = sorted(
        (item for item in candidates if int(item["company_id"]) not in decided),
        key=_sort_key,
    )
    for candidate in fresh:
        company_id = int(candidate["company_id"])
        if company_id in slotted:
            decisions.append({**candidate, "status": EXCLUDED, "reason": REASON_MUTEX, "rank": None})
            continue
        if remaining_official > 0:
            remaining_official -= 1
            slotted.add(company_id)
            decisions.append({**candidate, "status": OFFICIAL, "reason": None, "rank": next_rank})
            next_rank += 1
        elif remaining_waitlist > 0:
            remaining_waitlist -= 1
            slotted.add(company_id)
            decisions.append({**candidate, "status": WAITLIST, "reason": None, "rank": next_rank})
            next_rank += 1
        else:
            decisions.append({**candidate, "status": EXCLUDED, "reason": REASON_QUOTA, "rank": None})
    return decisions
