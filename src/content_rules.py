"""内容规则：按论文**内容**加分 / 剔除（与 ``ranking.py`` 的期刊档次加成并列）。

排序公式因此变成::

    最终分 = AI 相关性分 + 期刊档次加成 + 内容规则加成

⚠️ **两种加成一样，都只管排序、不管入选。** 能不能进邮件仍然只看 AI 分是否过
``AI_THRESHOLD``。这样"蹭到热词"或"发在顶刊"的论文不会被硬塞进邮件，
只是在同样相关时排得更靠前。

两个方向
--------
* **加分**（``config.BONUS_RULES`` + 主题自己的 ``bonuses``）
  命中就加分，如「固态电池 +1」「无负极 +3」。默认在 **标题 + 摘要** 上匹配
  （摘要里提到同样算数，宁滥勿缺 —— 反正只是排序）。
* **剔除**（``config.EXCLUDE_RULES`` + 主题自己的 ``exclude``）
  命中就直接丢出链路，**不进 AI 打分**（省钱），也不进邮件。
  默认**只看标题**：摘要是浓缩文本，"electrolyte additive" 这种词在**相关论文**里
  也常作为对比组出现，按摘要剔除会误杀；而漏掉的无关论文本来就会被 AI 打低分。
  确实想看摘要，在规则里显式写 ``"scope": "all"``。

匹配细节
--------
1. 先把文本**归一化**：转小写，把标点、连字符（含 ``-`` ``–`` ``—``）全部换成空格，
   压缩连续空格。于是 ``"Solid-state"`` / ``"solid state"`` / ``"solid—state"``
   都能被 ``"solid state"`` 命中。
2. 再按**词首对齐**匹配短语：``"solid state batter"`` 能命中 ``"solid-state batteries"``。
   ⚠️ 也因此短语别写太短（< 4 个字符），否则容易误伤（``"na"`` 会命中 ``"nanowire"``）。
3. 规则字段：``label`` / ``score`` / ``any``（必填，命中任一即可）/ ``all``（选填，必须全中）
   / ``unless``（选填，命中任一则**不算**命中）/ ``scope``（选填）。
"""

from __future__ import annotations

import functools
import logging
import re

from . import config

log = logging.getLogger(__name__)

#: 归一化：只保留小写字母、数字与汉字，其余一律换成空格
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

#: 排除规则的默认匹配范围（只看标题，见模块 docstring）
EXCLUDE_SCOPE = "title"

#: 加分规则的默认匹配范围（标题 + 摘要）
BONUS_SCOPE = "all"


# ---------------------------------------------------------------------------
# 文本归一化与匹配
# ---------------------------------------------------------------------------
def normalize(text: object) -> str:
    """把任意文本归一化成"小写 + 空格分隔"的形式，便于短语匹配。"""
    return _NORMALIZE_RE.sub(" ", str(text or "").lower()).strip()


@functools.lru_cache(maxsize=512)
def _pattern_regex(pattern: str) -> re.Pattern[str] | None:
    """把规则里的短语编译成"词首对齐"的正则（编译结果缓存，避免逐篇重复编译）。"""
    cleaned = normalize(pattern)
    if not cleaned:
        return None
    return re.compile(r"\b" + re.escape(cleaned))


def _hit(text: str, patterns: object) -> bool:
    """``patterns`` 里**任意一个**命中即返回 True。"""
    if not patterns:
        return False
    for pattern in patterns:  # type: ignore[union-attr]
        regex = _pattern_regex(str(pattern))
        if regex is not None and regex.search(text):
            return True
    return False


def _hit_all(text: str, patterns: object) -> bool:
    """``patterns`` 里**每一个**都要命中（空列表视为通过）。"""
    if not patterns:
        return True
    for pattern in patterns:  # type: ignore[union-attr]
        regex = _pattern_regex(str(pattern))
        if regex is None or not regex.search(text):
            return False
    return True


def rule_matches(rule: object, text: str) -> bool:
    """判断一条规则是否命中 ``text``（``text`` 必须已归一化）。"""
    if not isinstance(rule, dict) or not text:
        return False
    if not _hit(text, rule.get("any")):
        return False
    if not _hit_all(text, rule.get("all")):
        return False
    if _hit(text, rule.get("unless")):
        return False
    return True


def _work_texts(work: dict) -> tuple[str, str]:
    """返回 ``(标题, 标题+摘要)``，两者都已归一化。"""
    title = normalize(work.get("title"))
    abstract = normalize(work.get("abstract"))
    return title, f"{title} {abstract}".strip()


def _matching_rules(work: dict, rules: list[dict], default_scope: str) -> list[dict]:
    """按各条规则自己的 ``scope`` 挑出命中的规则。"""
    title, full = _work_texts(work)
    hits: list[dict] = []
    for rule in rules:
        scope = str(rule.get("scope") or default_scope).strip().lower()
        text = title if scope == "title" else full
        if rule_matches(rule, text):
            hits.append(rule)
    return hits


# ---------------------------------------------------------------------------
# 规则集合（全局 + 当前主题）
# ---------------------------------------------------------------------------
def bonus_rules(topic=None) -> list[dict]:
    """本轮生效的加分规则：全局的 + 该主题自己的（``score <= 0`` 的会被跳过）。"""
    rules = [
        rule
        for rule in (config.BONUS_RULES or [])
        if isinstance(rule, dict) and _score_of(rule) > 0
    ]
    if topic is not None:
        rules += [
            rule
            for rule in (getattr(topic, "bonuses", None) or [])
            if isinstance(rule, dict) and _score_of(rule) > 0
        ]
    return rules


def exclusion_rules(topic=None) -> list[dict]:
    """本轮生效的剔除规则：全局的 + 该主题自己的。"""
    rules = [rule for rule in (config.EXCLUDE_RULES or []) if isinstance(rule, dict)]
    if topic is not None:
        rules += [rule for rule in (getattr(topic, "exclude", None) or []) if isinstance(rule, dict)]
    return rules


def _score_of(rule: dict) -> int:
    try:
        return int(rule.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _label_of(rule: dict) -> str:
    return str(rule.get("label") or "内容").strip() or "内容"


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def matched_bonuses(work: dict, topic=None) -> list[tuple[str, int]]:
    """返回 ``[(label, score), ...]``，可能有多条同时命中（分数会累加）。"""
    return [
        (_label_of(rule), _score_of(rule))
        for rule in _matching_rules(work, bonus_rules(topic), BONUS_SCOPE)
    ]


def bonus_score(work: dict, topic=None) -> int:
    """内容加分合计。"""
    return sum(score for _, score in matched_bonuses(work, topic))


def exclusion_hit(work: dict, topic=None) -> str | None:
    """命中剔除规则时返回命中的规则名（多个用「、」连接），否则 ``None``。"""
    hits = _matching_rules(work, exclusion_rules(topic), EXCLUDE_SCOPE)
    if not hits:
        return None
    return "、".join(_label_of(rule) for rule in hits)


def partition_excluded(works: list[dict], topic=None) -> tuple[list[dict], list[dict]]:
    """按剔除规则把候选拆成 ``(保留, 剔除)``，被剔除的会写上 ``exclude_reason``。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for work in works:
        reason = exclusion_hit(work, topic)
        if reason:
            work["exclude_reason"] = reason
            dropped.append(work)
        else:
            kept.append(work)
    return kept, dropped


def log_excluded(dropped: list[dict], topic=None, prefix: str = "") -> None:
    """把剔除结果打进日志（每条只记标题，避免刷屏）。"""
    if not dropped:
        return
    scope = f"主题「{topic.name}」" if topic is not None else "本轮"
    log.info("%s规则剔除 %s 篇（%s不看：电解液工程 / 隔膜改性）", prefix, len(dropped), scope)
    for work in dropped:
        log.debug(
            "%s  [剔除·%s] %s", prefix, work.get("exclude_reason"), work.get("title")
        )


def describe_bonuses(topic=None) -> str:
    """一行文字列出生效的加分规则，用于 ``--show-config`` 与启动日志。"""
    rules = bonus_rules(topic)
    if not rules:
        return "（未配置任何内容加分）"
    return " ｜ ".join(f"{_label_of(rule)} +{_score_of(rule)}" for rule in rules)


def describe_excludes(topic=None) -> str:
    """一行文字列出生效的剔除规则。"""
    rules = exclusion_rules(topic)
    if not rules:
        return "（未配置任何剔除规则）"
    return " ｜ ".join(_label_of(rule) for rule in rules)


def summary(topic=None) -> str:
    """一句话说明内容规则，用于日志。"""
    return (
        f"内容加分 {describe_bonuses(topic)}；"
        f"剔除规则 {describe_excludes(topic)}"
        "（剔除只看标题，宁漏勿误杀）"
    )
