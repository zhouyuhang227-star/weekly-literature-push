"""可编辑的「检索方案」：``data/search_plan.json``。

为什么要有这一层
----------------
改造前，「检索范围」散落在 ``src/config.py`` 的多个段落里（JOURNALS / RESEARCH_TOPICS /
BONUS_RULES / AI_THRESHOLD / LOOKBACK_DAYS ...）。改一次范围要在 1400 行的代码里
翻半天，还容易改错层级 —— 用户的原话就是「检索范围也不好修改」。

现在的分工：

* ``config.py`` 仍然负责**基础设施**：期刊登记表（ISSN / DOI 前缀 / 档次加成）、
  默认阈值、默认时间窗、邮件与 PDF 参数。
* ``data/search_plan.json`` 负责**你要改的东西**：每个主题召回哪些词、只搜哪些刊、
  用哪些数据源、回看多少天、AI 门槛多少、加分/剔除/保底规则。
* 文件不存在时自动按 ``config.py`` 的 ``RESEARCH_TOPICS`` 生成同样一份方案 ——
  所以**不建这个文件也能照常运行**，行为与以前一致。

怎么用
------
    python -m src.main --write-plan     # 把当前生效的默认方案写成 JSON
    # 手动编辑 data/search_plan.json
    python -m src.main --show-plan      # 校验并打印最终生效的方案（离线）
    python -m src.main --dry-run -v     # 按新方案跑一轮预览

文件结构（``_comment`` 是给人看的，程序忽略）::

    {
      "_comment": "改完跑 python -m src.main --show-plan 校验",
      "version": 1,
      "topics": [
        {
          "name": "富锂锰正极",
          "key": "富锂锰正极",
          "search_terms": ["li-rich", "lithium-rich"],
          "keywords": ["..."],
          "description": "只看富锂锰基层状氧化物正极……",
          "journals": [],               // 空 = config.JOURNALS 全部
          "sources": [],                // 空 = config.DATA_SOURCES 全部
          "lookback_days": 0,           // 0 = config.LOOKBACK_DAYS (45)
          "lookback_days_first_run": 0, // 0 = config.LOOKBACK_DAYS_FIRST_RUN (90)
          "threshold_with_abstract": 0, // 0 = config.AI_THRESHOLD_WITH_ABSTRACT (60)
          "threshold_title_only": 0,    // 0 = config.AI_THRESHOLD_TITLE_ONLY (35)
          "bonuses": [], "exclude": [], "keep": []
        }
      ]
    }

``0`` / 空列表一律表示「用全局默认值」—— 这样全局默认改了，方案文件不用跟着改。
"""

from __future__ import annotations

import json
import logging
import os

from . import config
from .sources.base import canonical_source

log = logging.getLogger(__name__)

PLAN_VERSION = 1

#: 方案文件里允许出现的主题字段（写错字段名会被指出来，而不是静默忽略）
TOPIC_FIELDS = {
    "name",
    "key",
    "title",
    "topic_query",
    "topics",
    "search_terms",
    "keywords",
    "description",
    "exclude_note",
    "journals",
    "sources",
    "lookback_days",
    "lookback_days_first_run",
    "threshold_with_abstract",
    "threshold_title_only",
    "bonuses",
    "exclude",
    "keep",
    "_comment",
}


def plan_path(path: str | None = None) -> str:
    """方案文件路径。总是读**当前**的 ``config.PLAN_FILE``（测试可覆盖）。"""
    return path or config.PLAN_FILE


def _default_topic(topic: config.ResearchTopic) -> dict:
    """把 ``config.ResearchTopic`` 摊成方案文件里的一个主题。"""
    return {
        "name": topic.name,
        "key": topic.key,
        "title": topic.title,
        "topic_query": topic.topic_query,
        "topics": dict(topic.topics),
        "search_terms": list(topic.search_terms),
        "keywords": list(topic.keywords),
        "description": topic.description,
        "exclude_note": topic.exclude_note,
        "journals": list(topic.journals),
        "sources": list(topic.sources),
        "lookback_days": topic.lookback_days,
        "lookback_days_first_run": topic.lookback_days_first_run,
        "threshold_with_abstract": topic.threshold_with_abstract,
        "threshold_title_only": topic.threshold_title_only,
        "bonuses": list(topic.bonuses),
        "exclude": list(topic.exclude),
        "keep": list(topic.keep),
    }


def default_plan() -> dict:
    """按 ``config.py`` 生成一份完整的默认方案。"""
    return {
        "_comment": (
            "改完跑 python -m src.main --show-plan 校验；"
            "0 或空列表表示使用 config.py 的全局默认值"
        ),
        "version": PLAN_VERSION,
        "topics": [_default_topic(topic) for topic in config.active_research_topics()],
    }


def _fail(problems: list[str]) -> None:
    raise RuntimeError(
        "检索方案（%s）有 %s 处问题：\n  - %s"
        % (plan_path(), len(problems), "\n  - ".join(problems))
    )


def _as_str_list(value, field: str, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{field} 必须是字符串数组，现在是 {type(value).__name__}")
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            problems.append(f"{field} 里有非字符串项：{item!r}")
            continue
        if item.strip():
            out.append(item.strip())
    return out


def _as_rules(value, field: str, problems: list[str]) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{field} 必须是规则数组")
        return []
    rules = [rule for rule in value if isinstance(rule, dict)]
    if len(rules) != len(value):
        problems.append(f"{field} 里有非对象项（每条规则必须是 {{...}}）")
    problems.extend(config.validate_rule_list(rules, field))
    return rules


def _as_int(value, field: str, problems: list[str], *, lo: int = 0, hi: int = 10**9) -> int:
    if value in (None, ""):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        problems.append(f"{field} 必须是整数，现在是 {value!r}")
        return 0
    if not (lo <= number <= hi):
        problems.append(f"{field}={number} 超出允许范围 {lo}–{hi}")
    return number


def validate_plan(data) -> list[str]:
    """校验方案，返回问题列表（空列表 = 全部合法）。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"顶层必须是对象，现在是 {type(data).__name__}"]

    version = data.get("version", PLAN_VERSION)
    if version != PLAN_VERSION:
        problems.append(f"version={version!r} 不认识（当前支持 {PLAN_VERSION}）")

    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        return problems + ["topics 必须是非空数组（至少一个主题）"]

    names: list[str] = []
    keys: list[str] = []
    for index, item in enumerate(topics, start=1):
        label = f"topics 第 {index} 项"
        if not isinstance(item, dict):
            problems.append(f"{label} 必须是对象")
            continue
        unknown = sorted(set(item) - TOPIC_FIELDS)
        if unknown:
            problems.append(f"{label} 有未知字段：{'、'.join(unknown)}（拼错了？）")

        name = str(item.get("name") or "").strip()
        if not name:
            problems.append(f"{label} 缺少 name")
        else:
            names.append(name)
            label = f"主题「{name}」"
        key = str(item.get("key") or name or "").strip()
        if key:
            keys.append(key)

        for field in ("search_terms", "keywords", "journals", "sources"):
            if field in item:
                _as_str_list(item.get(field), f"{label} 的 {field}", problems)

        for field in ("bonuses", "exclude", "keep"):
            if field in item:
                _as_rules(item.get(field), f"{label} 的 {field}", problems)

        for field in ("lookback_days", "lookback_days_first_run"):
            if field in item:
                _as_int(
                    item.get(field), f"{label} 的 {field}", problems, lo=0, hi=3650
                )
        for field in ("threshold_with_abstract", "threshold_title_only"):
            if field in item:
                _as_int(item.get(field), f"{label} 的 {field}", problems, lo=0, hi=100)

        with_abstract = _as_int(
            item.get("threshold_with_abstract"), "", [], lo=0, hi=100
        )
        title_only = _as_int(item.get("threshold_title_only"), "", [], lo=0, hi=100)
        if with_abstract and title_only and title_only > with_abstract:
            problems.append(
                f"{label} 的 threshold_title_only（{title_only}）不能高于 "
                f"threshold_with_abstract（{with_abstract}）"
            )

        journals = [str(x) for x in (item.get("journals") or []) if isinstance(x, str)]
        for journal_name in journals:
            if journal_name not in config.JOURNALS:
                problems.append(
                    f"{label} 的期刊 {journal_name!r} 不在 config.JOURNALS 里"
                    "（新增期刊需要先补 ISSN）"
                )
        for source in [str(x) for x in (item.get("sources") or []) if isinstance(x, str)]:
            try:
                canonical_source(source)
            except ValueError as exc:
                problems.append(f"{label} 的数据源 {source!r} 不认识：{exc}")

    if len(set(names)) != len(names):
        problems.append("主题 name 有重复（状态文件的分区键不能重复）")
    if len(set(keys)) != len(keys):
        problems.append("主题 key 有重复（状态文件的分区键不能重复）")
    return problems


def load_plan(path: str | None = None) -> tuple[dict, bool]:
    """读取方案，返回 ``(方案, 是否来自文件)``。

    文件不存在 → 返回 config 生成的默认方案（``False``）。
    文件存在但损坏 → **直接报错**：静默回退到默认方案等于悄悄改变检索范围。
    """
    target = plan_path(path)
    if not os.path.exists(target):
        return default_plan(), False
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"检索方案文件读不了：{target}（{exc}）") from exc

    problems = validate_plan(data)
    if problems:
        _fail(problems)
    return data, True


def _topic_from_dict(item: dict) -> config.ResearchTopic:
    return config.ResearchTopic(
        name=str(item.get("name") or ""),
        topic_query=str(item.get("topic_query") or ""),
        search_terms=[str(term) for term in (item.get("search_terms") or [])],
        keywords=[str(word) for word in (item.get("keywords") or [])],
        description=str(item.get("description") or ""),
        exclude_note=str(item.get("exclude_note") or ""),
        topics=dict(item.get("topics") or {}),
        title=str(item.get("title") or ""),
        key=str(item.get("key") or ""),
        bonuses=[rule for rule in (item.get("bonuses") or []) if isinstance(rule, dict)],
        exclude=[rule for rule in (item.get("exclude") or []) if isinstance(rule, dict)],
        keep=[rule for rule in (item.get("keep") or []) if isinstance(rule, dict)],
        journals=[str(name) for name in (item.get("journals") or [])],
        sources=[str(name) for name in (item.get("sources") or [])],
        lookback_days=config._int_or_zero(item.get("lookback_days")),
        lookback_days_first_run=config._int_or_zero(item.get("lookback_days_first_run")),
        threshold_with_abstract=config._int_or_zero(item.get("threshold_with_abstract")),
        threshold_title_only=config._int_or_zero(item.get("threshold_title_only")),
    )


def effective_topics(path: str | None = None) -> list[config.ResearchTopic]:
    """本轮真正生效的主题列表（方案文件优先，否则用 config）。"""
    data, from_file = load_plan(path)
    topics = [_topic_from_dict(item) for item in data.get("topics") or []]
    if from_file:
        log.info(
            "检索方案：使用 %s（%s 个主题：%s）",
            plan_path(path),
            len(topics),
            "、".join(topic.name for topic in topics),
        )
    else:
        log.info(
            "检索方案：未找到 %s，按 src/config.py 的 RESEARCH_TOPICS 运行"
            "（想改成可编辑文件：python -m src.main --write-plan）",
            plan_path(path),
        )
    return topics


def write_plan(path: str | None = None, *, overwrite: bool = False) -> str:
    """把当前默认方案写成 JSON 文件，返回路径。

    文件已存在且 ``overwrite=False`` 时**不覆盖**（那可能是用户改了很久的文件）。
    """
    target = plan_path(path)
    if os.path.exists(target) and not overwrite:
        raise RuntimeError(
            f"方案文件已存在：{target}\n"
            "  想重新生成一份默认方案，加 --force 覆盖（会丢掉手改内容）。"
        )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    payload = default_plan()
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, target)
    return target


def describe(plan: dict) -> str:
    """一行摘要（日志用）。"""
    topics = plan.get("topics") or []
    return f"{len(topics)} 个主题：" + "、".join(
        str(item.get("name") or "（未命名）") for item in topics if isinstance(item, dict)
    )
