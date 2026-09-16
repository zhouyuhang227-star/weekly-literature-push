"""加权排序：期刊档次加成 **+** 内容规则加成。

规则的数字都在 ``config.py`` 里（``JOURNAL_TIERS`` / ``BONUS_RULES`` / ``KEEP_RULES``）：

    最终分 = AI 相关性分（0-100） + 期刊档次加成 + 内容规则加成

两种加成**都只影响排序，不影响入选** —— 是否进邮件仍然只看 AI 分是否过
``AI_THRESHOLD``。这样"顶刊的低相关论文"和"蹭到热词的论文"都不会挤掉
"普通刊的高相关论文"，只是同样相关时排得更靠前。

（唯一能改变"入选"的是两条**保底**通道：``config.KEEP_RULES`` 的关键词保底，
以及 AI 判定「无负极 + 体系对口」的保底。它们的生效点在 :mod:`src.ai_matcher`，
本模块只负责把它们置顶。）

两类加成的分工：
* **期刊档次加成**（``JOURNAL_TIERS``）：这篇发在哪本刊。
* **内容规则加成**（``config.BONUS_RULES`` + 主题的 ``bonuses``）：这篇写了什么，
  由 :mod:`src.content_rules` 按关键词规则判定，可命中多条累加（如
  「固态电池 +1」+「固态聚合物电解质 +1」）。

为什么按 ISSN 查而不是按期刊名查：OpenAlex 返回的 ``display_name`` 常与
配置里的写法不同（"Angewandte Chemie International Edition" vs
"Angewandte Chemie Int. Ed."、"Advanced Materials (Deerfield Beach, Fla.)" 等），
按名字匹配会漏。按 ISSN 查是精确的，期刊名的表只作兜底。
"""

from __future__ import annotations

import logging

from . import config, content_rules

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


def content_items(work: dict) -> list[tuple[str, int]]:
    """内容加分明细 ``[(label, score), ...]``。

    优先用 :func:`annotate` 写好的 ``content_bonus_detail``；只手工给了
    ``content_bonus`` 时退化成一项 ``("内容", N)``。
    """
    items: list[tuple[str, int]] = []
    for item in work.get("content_bonus_detail") or []:
        try:
            label, score = item
        except (TypeError, ValueError):
            continue
        label = str(label).strip()
        if label:
            items.append((label, int(score)))
    if not items:
        bonus = int(work.get("content_bonus") or 0)
        if bonus > 0:
            items.append(("内容", bonus))
    return items


def content_parts(work: dict) -> list[str]:
    """内容加分的明细文字，如 ``["固态电池 1", "无负极 10"]``。"""
    return [f"{label} {score}" for label, score in content_items(work)]


def annotate(works: list[dict], topic=None) -> list[dict]:
    """就地写入加权字段并算出 ``final_score``。

    写入的字段：``journal_tier`` / ``journal_bonus`` /
    ``content_bonus`` / ``content_bonus_detail`` / ``final_score`` / ``force_keep``。
    """
    for work in works:
        tier, journal_bonus = journal_tier(work)
        hits = content_rules.matched_bonuses(work, topic)
        content_bonus = sum(score for _label, score in hits)
        work["journal_tier"] = tier
        work["journal_bonus"] = journal_bonus
        work["content_bonus"] = content_bonus
        work["content_bonus_detail"] = [[label, score] for label, score in hits]
        work["final_score"] = int(work.get("ai_score") or 0) + journal_bonus + content_bonus
        # 硬保底：命中保底的论文置顶（排序里再抬一手，保证不仅"留下"
        # 而且第一眼就看得见）。邮件卡片上会打一个绿色保底标签解释为什么它在最上面，
        # 否则"AI 55 分排在 AI 98 分前面"看起来就像个 bug。
        #
        # 保底理由有三个来源，按"谁先写谁优先"取：
        #   1. 已经写在 work 上的（主流程 content_rules.mark_kept、或 ai_matcher 的 AI 判定）
        #   2. 关键词保底 keep_hit
        #   3. AI 判定保底 ai_keep_hit（关键词一个都没命中、靠 AI 读摘要读出来的）
        # 顺手把 keep_reason 也写上：主流程里 content_rules 早就写过了，
        # 但只调 ranking.rank() 时（测试、将来可能的单独重排）没有，
        # 而邮件/PDF 标签要显示这个规则名，缺了就只能显示一句无信息量的兜底文案。
        keep_reason = (
            work.get("keep_reason")
            or content_rules.keep_hit(work, topic)
            or content_rules.ai_keep_hit(work)
        )
        if keep_reason:
            work["keep_reason"] = keep_reason
        work["force_keep"] = bool(keep_reason)
    return works


def sort_key(work: dict) -> tuple[int, int, int, str]:
    """排序键：硬保底 → 最终分 → AI 分 → 发表日期。

    保底位放在最前面是有意的：老板要盯的方向可能 AI 打分并不高（判据里本来就写了
    "不看电解液工程"），只靠内容加分不足以保证它挤进正文前 20 篇而不会掉进附件。

    后面两项：带上 AI 分是为了"加成追平"时仍按真实相关性分先后；
    带上日期是为了完全同分时结果稳定（不会每次运行顺序都变）。
    """
    return (
        1 if work.get("force_keep") else 0,
        int(work.get("final_score") or 0),
        int(work.get("ai_score") or 0),
        str(work.get("pub_date") or ""),
    )


def rank(works: list[dict], topic=None) -> list[dict]:
    """标注加成分并按最终分降序排列（返回新列表，原列表内容不变）。"""
    annotate(works, topic)
    return sorted(works, key=sort_key, reverse=True)


def breakdown(work: dict) -> str:
    """一行加分明细，用于邮件与日志，如 ``AI 62 + 大子刊 9 + 固态电池 1 = 72``。"""
    ai = int(work.get("ai_score") or 0)
    journal_bonus = int(work.get("journal_bonus") or 0)
    parts = [f"AI {ai}"]
    if journal_bonus > 0:
        parts.append(f"{work.get('journal_tier') or UNRANKED_TIER} {journal_bonus}")
    parts.extend(content_parts(work))
    if len(parts) == 1:
        return f"AI {ai}"
    stored = work.get("final_score")
    if stored is not None:
        total = int(stored)
    else:
        total = ai + journal_bonus + int(work.get("content_bonus") or 0)
    return " + ".join(parts) + f" = {total}"


def tiers() -> list[tuple[str, int]]:
    """按高低顺序返回 ``[(档次名, 加成), ...]``。"""
    return [(tier, bonus) for tier, (bonus, _names) in config.JOURNAL_TIERS.items()]


def forced_count(works: list[dict]) -> int:
    """其中有几篇是硬保底（邮件页头要拿它决定要不要加提示）。"""
    return sum(1 for work in works if work.get("force_keep"))


def describe() -> str:
    """一行文字说明排序规则，用于启动日志与 ``--show-config``。

    内容加分的明细由 :func:`content_rules.describe_bonuses` 单独打印
    （有全局的也有每个主题自己的，混在一行说不清）。
    """
    parts = [f"{tier} +{bonus}" for tier, bonus in tiers()]
    parts.append(f"{UNRANKED_TIER} +{UNRANKED_BONUS}")
    return (
        "最终分 = AI 相关性分 + 期刊加成 + 内容加分，按最终分降序"
        "（期刊：" + " > ".join(parts) + "）；命中保底规则的论文无条件置顶"
    )
