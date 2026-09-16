"""期刊档次加权排序。

规则（可在 ``config.JOURNAL_TIERS`` 里改数字）：

    最终分 = AI 相关性分（0-100） + 期刊档次加成

加成**只影响排序，不影响入选** —— 是否进邮件仍然只看 AI 分是否过 ``AI_THRESHOLD``。
这样"顶刊的低相关论文"不会挤掉"普通刊的高相关论文"，只是同样相关时顶刊排前面。

为什么按 ISSN 查而不是按期刊名查：OpenAlex 返回的 ``display_name`` 常与
配置里的写法不同（"Angewandte Chemie International Edition" vs
"Angewandte Chemie Int. Ed."、"Advanced Materials (Deerfield Beach, Fla.)" 等），
按名字匹配会漏。按 ISSN 查是精确的，期刊名的表只作兜底。
"""

from __future__ import annotations

import logging

from . import config

log = logging.getLogger(__name__)

#: 不在档次表里的期刊
UNRANKED_TIER = "其他"
UNRANKED_BONUS = 0


def journal_tier(work: dict) -> tuple[str, int]:
    """返回 ``(档次名, 加成)``；不在档次表里的期刊返回 ``("其他", 0)``。"""
    issn = str(work.get("issn") or "").strip().lower()
    if issn:
        entry = config.JOURNAL_RANK_BY_ISSN.get(issn)
        if entry is not None:
            return entry

    name = str(work.get("journal") or "").strip()
    entry = config.JOURNAL_RANK.get(name)
    if entry is not None:
        return entry

    return UNRANKED_TIER, UNRANKED_BONUS


def annotate(works: list[dict]) -> list[dict]:
    """就地写入 ``journal_tier`` / ``journal_bonus`` / ``final_score`` 三个字段。"""
    for work in works:
        tier, bonus = journal_tier(work)
        work["journal_tier"] = tier
        work["journal_bonus"] = bonus
        work["final_score"] = int(work.get("ai_score") or 0) + bonus
    return works


def sort_key(work: dict) -> tuple[int, int, str]:
    """排序键：最终分 → AI 分 → 发表日期。

    带上 AI 分是为了"期刊加成追平"时仍按真实相关性分先后；
    带上日期是为了完全同分时结果稳定（不会每次运行顺序都变）。
    """
    return (
        int(work.get("final_score") or 0),
        int(work.get("ai_score") or 0),
        str(work.get("pub_date") or ""),
    )


def rank(works: list[dict]) -> list[dict]:
    """标注加成分并按最终分降序排列（返回新列表，原列表内容不变）。"""
    annotate(works)
    return sorted(works, key=sort_key, reverse=True)


def breakdown(work: dict) -> str:
    """一行加分明细，用于邮件与日志。"""
    ai = int(work.get("ai_score") or 0)
    bonus = int(work.get("journal_bonus") or 0)
    tier = work.get("journal_tier") or UNRANKED_TIER
    if bonus > 0:
        return f"AI {ai} + {tier} {bonus} = {int(work.get('final_score') or ai + bonus)}"
    return f"AI {ai}"


def tiers() -> list[tuple[str, int]]:
    """按高低顺序返回 ``[(档次名, 加成), ...]``。"""
    return [(tier, bonus) for tier, (bonus, _names) in config.JOURNAL_TIERS.items()]


def describe() -> str:
    """一行文字说明排序规则，用于启动日志与 ``--show-config``。"""
    parts = [f"{tier} +{bonus}" for tier, bonus in tiers()]
    parts.append(f"{UNRANKED_TIER} +{UNRANKED_BONUS}")
    return "最终分 = AI 相关性分 + 期刊加成，按最终分降序（" + " > ".join(parts) + "）"
