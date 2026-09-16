"""内容规则：按论文**内容**加分 / 剔除 / 保底（与 ``ranking.py`` 的期刊档次加成并列）。

排序公式因此变成::

    最终分 = AI 相关性分 + 期刊档次加成 + 内容规则加成

⚠️ **两种加成一样，都只管排序、不管入选。** 能不能进邮件仍然只看 AI 分是否过
``AI_THRESHOLD``。这样"蹭到热词"或"发在顶刊"的论文不会被硬塞进邮件，
只是在同样相关时排得更靠前。

**唯一能改变"入选"的是保底**（``config.KEEP_RULES`` + 主题自己的 ``keep``，
外加一条 AI 侧保底 :func:`ai_keep_hit`），见下面第三个方向。

三个方向
--------
* **加分**（``config.BONUS_RULES`` + 主题自己的 ``bonuses``）
  命中就加分，如「固态电池 +1」「无负极 +10」。默认在 **标题 + 摘要** 上匹配
  （摘要里提到同样算数，宁滥勿缺 —— 反正只是排序）。
* **剔除**（``config.EXCLUDE_RULES`` + 主题自己的 ``exclude``）
  命中就直接丢出链路，**不进 AI 打分**（省钱），也不进邮件。
  默认**只看标题**：摘要是浓缩文本，"electrolyte additive" 这种词在**相关论文**里
  也常作为对比组出现，按摘要剔除会误杀；而漏掉的无关论文本来就会被 AI 打低分。
  确实想看摘要，在规则里显式写 ``"scope": "all"``。
* **硬保底**（``config.KEEP_RULES`` + 主题自己的 ``keep``）
  命中就**强制进邮件**，且**免于上面所有剔除规则**。
  还有一条**AI 侧保底**（:func:`ai_keep_hit`）走的是同一条通道：
  AI 从摘要里判定"这篇就是无负极、而且正极是富锂锰/钠电"时同样强推。
  为什么词表之外还需要它：无负极是一种**电芯构型**，往往不是论文的研究重点，
  标题里可能一个字都不写，摘要里才以「裸 Cu 集流体」「负极过量≈0」的形式出现 ——
  关键词表对这种写法无计可施。

为什么需要保底这一层
--------------------
有些方向是"无论怎么判都要看"的（例如老板指定要盯的无负极构型）。
而这类论文常常正好撞在剔除规则上 —— 无负极钠电论文很多以「电解液设计/工程」
为主题，直接命中 ``EXCLUDE_RULES`` 里的「电解液工程」，在**还没进 AI 打分**时
就被丢掉了，加分规则根本来不及生效。只在 ``unless`` 里打补丁也不够：
那只能免掉**一条**剔除规则，而且免完仍要过 ``AI_THRESHOLD``，
而 AI 恰恰被告知"不看电解液工程"。所以单开这一层。

匹配细节
--------
1. 先把文本**归一化**：转小写，把标点、连字符（含 ``-`` ``–`` ``—``）全部换成空格，
   压缩连续空格。于是 ``"Solid-state"`` / ``"solid state"`` / ``"solid—state"``
   都能被 ``"solid state"`` 命中。
2. 再按**词首对齐**匹配短语：``"solid state batter"`` 能命中 ``"solid-state batteries"``。
   ⚠️ 也因此短语别写太短（< 4 个字符），否则容易误伤（``"na"`` 会命中 ``"nanowire"``）。
3. 规则字段：``label`` / ``score`` / ``any``（必填，命中任一即可）/ ``all``（选填，必须全中）
   / ``unless``（选填，命中任一则**不算**命中）/ ``require``（选填，"必须命中"的短语**组**：
   组之间 AND、组内 OR）/ ``scope``（选填）。
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

#: 保底规则的默认匹配范围。
#: 用 "all"（标题+摘要）而不是排除规则的 "title"，因为判断的是**要不要留**：
#: 只在摘要里提到无负极的论文同样属于这个方向，漏掉比多留一篇严重得多。
#: 代价是可能把"摘要里拿无负极当对照组"的论文也强推进邮件，想收紧就在规则里写
#: "scope": "title"。
KEEP_SCOPE = "all"


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


def _hit_required(text: str, groups: object) -> bool:
    """``groups`` 是"必须命中"的短语**组**列表：**每一组**都要命中，组内任意一个即可。

    ``all`` 表达不了"A 或 B 至少命中一个"，而保底恰恰需要：
    无负极 **且**（富锂锰 **或** 钠电）。见 ``config.KEEP_RULES`` 的 ``require``。
    形状写错（例如组写成了字符串）只会"永远不命中"，所以 ``config.validate_rule_list``
    会专门查它。
    """
    if not groups:
        return True
    for group in groups:  # type: ignore[union-attr]
        if not _hit(text, group):
            return False
    return True


def rule_matches(rule: object, text: str) -> bool:
    """判断一条规则是否命中 ``text``（``text`` 必须已归一化）。"""
    if not isinstance(rule, dict) or not text:
        return False
    if not _hit(text, rule.get("any")):
        return False
    if not _hit_required(text, rule.get("require")):
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


def keep_rules(topic=None) -> list[dict]:
    """本轮生效的硬保底规则：全局的 + 该主题自己的。

    与 ``bonus_rules`` 不同，这里**不过滤 score**：保底规则本来就
    不靠分数起作用（它是入选开关，不是排序权重）。
    """
    rules = [rule for rule in (config.KEEP_RULES or []) if isinstance(rule, dict)]
    if topic is not None:
        rules += [rule for rule in (getattr(topic, "keep", None) or []) if isinstance(rule, dict)]
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
    """命中剔除规则时返回命中的规则名（多个用「、」连接），否则 ``None``。

    ⚠️ **命中保底规则的论文一律返回 ``None``**（即"不剔除"）。
    免剔除是写在这里而不是写在调用方，是为了让所有调用点（``partition_excluded``、
    日志、将来的新流程）都不可能绕过去 —— 保底一旦能被绕过就等于没有。
    """
    if keep_hit(work, topic):
        return None
    hits = _matching_rules(work, exclusion_rules(topic), EXCLUDE_SCOPE)
    if not hits:
        return None
    return "、".join(_label_of(rule) for rule in hits)


def keep_matched(work: dict, topic=None) -> list[dict]:
    """返回命中的保底规则（可能有多条）。"""
    return _matching_rules(work, keep_rules(topic), KEEP_SCOPE)


def keep_hit(work: dict, topic=None) -> str | None:
    """命中保底规则时返回规则名（多个用「、」连接），否则 ``None``。"""
    hits = keep_matched(work, topic)
    if not hits:
        return None
    return "、".join(_label_of(rule) for rule in hits)


def is_kept(work: dict, topic=None) -> bool:
    """该篇是否被保底（命中保底规则）。"""
    return bool(keep_matched(work, topic))


def ai_keep_hit(work: dict) -> str | None:
    """AI 判定「无负极 + 体系对口」时返回保底理由，否则 ``None``。

    这是**关键词保底之外的补充**（是一条独立的保底通道，与 ``KEEP_RULES`` 并列）。
    为什么需要：``ANODE_FREE_TERMS`` 只能做字面匹配，而"是不是无负极"经常要靠读懂
    **电芯构型**才能判断（「裸 Cu 集流体」「负极过量≈0」）—— 那类论文的关键词可能
    一个都不命中。AI 本来就通读了摘要（口径见 ``config.AI_ANODE_FREE_HINT``），
    让它顺手判一下即可：判为无负极 **且** 正极体系是富锂锰/钠电 → 视同命中保底
    （免 AI 阈值 + 置顶），且解读里会点名「无负极」。

    ⚠️ 前提是那篇论文**过了剔除、并且已经送给 AI 打过分**：被剔除规则丢掉的论文
    根本走不到 AI，AI 也就没机会捞它 —— 那条路仍然只能靠关键词保底兜。
    """
    if not work.get("anode_free"):
        return None
    system = str(work.get("cathode_system") or "").strip().lower()
    label = config.KEEP_CATHODE_SYSTEMS.get(system)
    if not label:
        return None
    return f"无负极（AI 判定 · {label}）"


def mark_kept(works: list[dict], topic=None) -> list[dict]:
    """就地给命中保底的论文写上 ``keep_reason``，并返回命中保底的那些。

    ``keep_reason`` 会被邮件渲染成标签、也会进日志，所以这里就写好，
    免得下游各自重复匹配一遍（规则匹配走的是缓存正则，但仍不必白跑）。
    """
    kept: list[dict] = []
    for work in works:
        reason = keep_hit(work, topic)
        if reason:
            work["keep_reason"] = reason
            kept.append(work)
    return kept


def partition_excluded(works: list[dict], topic=None) -> tuple[list[dict], list[dict]]:
    """按剔除规则把候选拆成 ``(保留, 剔除)``，被剔除的会写上 ``exclude_reason``。

    命中**保底规则**的论文永远不会出现在``剔除``这一侧（见 :func:`exclusion_hit`），
    同时会被写上 ``keep_reason``。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for work in works:
        reason = keep_hit(work, topic)
        if reason:
            work["keep_reason"] = reason
            kept.append(work)
            continue
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
    log.info("%s规则剔除 %s 篇（%s不看：%s）", prefix, len(dropped), scope, describe_excludes(topic))
    for work in dropped:
        log.debug(
            "%s  [剔除·%s] %s", prefix, work.get("exclude_reason"), work.get("title")
        )


def log_kept(kept: list[dict], prefix: str = "") -> None:
    """把保底命中的论文打进日志。**用 INFO 级**：这是"老板要盯的方向"，
    得能在 Actions 日志里一眼看到，而不是埋在 DEBUG 里。"""
    if not kept:
        return
    log.info("%s规则保底 %s 篇（命中即强制进邮件，AI 分再低也不丢）：", prefix, len(kept))
    for work in kept:
        log.info(
            "%s  [保底·%s] %s", prefix, work.get("keep_reason"), work.get("title")
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


def describe_keeps(topic=None) -> str:
    """一行文字列出生效的硬保底规则（带命中短语 + 体系闸门，方便核对词表）。"""
    rules = keep_rules(topic)
    if not rules:
        return "（未配置任何保底规则）"
    parts: list[str] = []
    for rule in rules:
        terms = "、".join(str(p) for p in (rule.get("any") or [])[:3])
        more = " 等" if len(rule.get("any") or []) > 3 else ""
        text = f"{_label_of(rule)}（命中：{terms}{more}"
        # 体系闸门也要回显："保底为什么不触发"最常见的原因就是闸门没命中
        #（例如一篇锂硫的阳极无负极）。
        gates = [group for group in (rule.get("require") or []) if group]
        if gates:
            shown: list[str] = []
            for group in gates:
                words = " / ".join(str(p) for p in list(group)[:3])
                shown.append(words + (" 等" if len(group) > 3 else ""))
            text += "；且须命中：" + "、".join(shown)
        parts.append(text + "）")
    return " ｜ ".join(parts)


def summary(topic=None) -> str:
    """一句话说明内容规则，用于日志。"""
    return (
        f"内容加分 {describe_bonuses(topic)}；"
        f"剔除规则 {describe_excludes(topic)}（剔除只看标题，宁漏勿误杀）；"
        f"硬保底 {describe_keeps(topic)}（免剔免阈值，强制进邮件）"
    )
