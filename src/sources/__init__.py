"""多数据源并集检索。

用法
----
``fetch_works()`` 是本包唯一的对外入口，签名与原来的
``openalex_client.fetch_works`` 兼容，只是多了一个 ``sources=`` 参数，
返回值从「work 列表」变成 ``FetchResult``（内含 work 列表 + 每个源的表现）。

设计要点
--------
**为什么是并集而不是自动切换**
    自动切换（主源挂了才用备用）只会在主源挂掉的那一轮生效，而且「挂了」
    往往表现为「静默返回空」—— 那正是本项目吃过的最贵的亏。并集则是每轮都
    问所有源再合并，任何一个源出问题都只是少一份结果，而不是整轮断供。

**合并的收益不只是容灾**
    不同源对摘要的覆盖完全不同（OpenAlex 部分缺失、Crossref 在 Wiley 系刊接近
    100%、S2 约 80-94%）。合并时**取最长的摘要**，等于免费提升了 AI 打分的准确性。

**失败必须看得见**
    * 某个源失败 → 其余源照常工作，日志记 ERROR，并把失败原因写进**邮件页头**；
    * **源成功但结果不完整** → 也写进邮件页头（如 Crossref 有 1/15 本刊查询失败）。
      这种情形最阴险：源状态是 ok，页头上只是一个偏小的篇数，看不出少了一整本刊；
    * 全部源失败 → 直接 ``raise``，让 Actions 变红，而不是发一封"本周无新文献"。
"""

from __future__ import annotations

import inspect
import logging

from .. import config
from ..openalex_client import effective_keywords
from . import crossref as _crossref
from . import openalex as _openalex
from . import semantic_scholar as _semantic_scholar
from .base import (
    ALL_SOURCES,
    CROSSREF,
    OPENALEX,
    SEMANTIC_SCHOLAR,
    FetchResult,
    SourceReport,
    canonical_source,
    merge_works,
    source_label,
)

log = logging.getLogger(__name__)

#: 源 id → 适配器的 ``fetch`` 函数
ADAPTERS: dict[str, object] = {
    OPENALEX: _openalex.fetch,
    CROSSREF: _crossref.fetch,
    SEMANTIC_SCHOLAR: _semantic_scholar.fetch,
}

__all__ = [
    "ALL_SOURCES",
    "CROSSREF",
    "OPENALEX",
    "SEMANTIC_SCHOLAR",
    "FetchResult",
    "SourceReport",
    "canonical_source",
    "enabled_sources",
    "fetch_works",
    "source_label",
]


def enabled_sources(names=None) -> list[str]:
    """确定本轮启用哪些源。

    ``names`` 为空时读 ``config.DATA_SOURCES``；可以是列表，也可以是
    ``"openalex,crossref"`` 这样的字符串（命令行直接传）。**去重且保持顺序**。
    出现不认识的源名会立刻报错，而不是安静地忽略。
    """
    if names is None:
        raw: list = list(getattr(config, "DATA_SOURCES", ALL_SOURCES) or [])
    elif isinstance(names, str):
        raw = [part.strip() for part in names.replace(";", ",").split(",")]
    else:
        raw = list(names)

    ordered: list[str] = []
    for item in raw:
        if not str(item).strip():
            continue
        name = canonical_source(str(item))
        if name not in ordered:
            ordered.append(name)

    if not ordered:
        raise RuntimeError(
            "没有启用任何数据源 —— 请检查 config.DATA_SOURCES（不能为空）"
        )
    return ordered


def _notes_sink(fn) -> list[str] | None:
    """适配器接不接受 ``notes=`` 告警通道？接受就返回一个空列表，否则 ``None``。

    为什么要探测而不是直接传：`ADAPTERS` 在测试里会被换成测试桩
    （多为 ``lambda *_a, **_k``），给每个桩都加一个用不上的参数纯属噪音；
    而带 ``**kwargs`` 的桩会把 ``notes`` 默默吃掉、告警就丢了，
    所以**只有显式声明了 ``notes`` 的适配器才传**。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # 拿不到签名的内建/C 可调用对象
        return None
    return [] if "notes" in params else None


def fetch_works(
    keywords: list[str] | None,
    lookback_days: int,
    max_works: int | None = None,
    *,
    mode: str | None = None,
    topic=None,
    sources=None,
) -> FetchResult:
    """多源并集检索。

    :param keywords: 命令行 ``--keywords`` 的覆盖值。**为空是常态** ——
                     真正生效的是主题自己的 ``search_terms``（经
                     ``openalex_client.effective_keywords`` 解析），
                     与 OpenAlex 走完全同一套优先级。
    :param max_works: 单源上限与合并后的总上限（默认 ``config.MAX_WORKS_FETCH``）。
    :param mode: 检索模式，透传给 OpenAlex。
    :param topic: 主题对象，透传给 OpenAlex。
    :param sources: 覆盖 ``config.DATA_SOURCES``（列表或逗号分隔字符串）。
    :raises RuntimeError: 全部数据源都失败时抛出（**绝不返回空结果假装成功**）。
    """
    names = enabled_sources(sources)
    cap = max(1, int(max_works or config.MAX_WORKS_FETCH))

    # ★ 召回词必须和 OpenAlex 走同一套优先级（--keywords > 主题 search_terms >
    #   主题 keywords > USER_KEYWORDS）。曾经这里直接用 keywords 参数，
    #   而命令行通常不传 --keywords ⇒ 词表是空的 ⇒ Crossref 与 S2 被"静默跳过"，
    #   多源实际上原地失效（而且日志里看起来一切正常）。
    terms = [
        str(term).strip()
        for term in effective_keywords(keywords, topic)
        if str(term).strip()
    ]
    mode_norm = str(mode or getattr(config, "RETRIEVAL_MODE", "keyword") or "").lower()
    if mode_norm == "topic":
        # topic 模式是"用 OpenAlex 语义主题召回"，备用源没有语义主题这个概念；
        # 拿字面词去猜反而会引入用户没要的结果 ⇒ 明确跳过并记清楚原因。
        terms = []

    log.info(
        "多源并集检索：%s（时间窗 %s 天）",
        " + ".join(source_label(name) for name in names),
        lookback_days,
    )

    reports: list[SourceReport] = []
    groups: list[tuple[str, list[dict]]] = []

    for name in names:
        label = source_label(name)

        if name != OPENALEX and not terms:
            reason = (
                "topic 模式只查 OpenAlex 语义主题，本源不支持"
                if mode_norm == "topic"
                else "该源只支持字面关键词检索，当前主题没有可用的召回词"
            )
            log.warning("[%s] 本轮跳过：%s", label, reason)
            reports.append(SourceReport(name=name, status="skipped", error=reason))
            continue

        notes = _notes_sink(ADAPTERS[name])
        extra = {"notes": notes} if notes is not None else {}
        try:
            if name == OPENALEX:
                works = ADAPTERS[name](
                    terms, lookback_days, cap, mode=mode, topic=topic, **extra
                )
            else:
                works = ADAPTERS[name](terms, lookback_days, cap, **extra)
        except Exception as exc:  # noqa: BLE001 - 单源失败不该拖垮整轮
            log.error("[%s] 检索失败：%s", label, exc)
            reports.append(SourceReport(name=name, status="failed", error=str(exc)))
            continue

        if len(works) > cap:
            log.info("[%s] 单源结果 %s 篇，截断到 %s 篇", label, len(works), cap)
            works = works[:cap]

        log.info("[%s] 返回 %s 篇", label, len(works))
        for note in notes or []:
            # 源成功了但不完整：既记日志，也随 SourceReport 进邮件页头
            log.warning("[%s] 结果不完整：%s", label, note)
        reports.append(
            SourceReport(name=name, status="ok", count=len(works), notes=list(notes or []))
        )
        groups.append((name, works))

    if not groups:
        detail = "；".join(f"{r.label}：{r.error}" for r in reports) or "没有启用任何数据源"
        raise RuntimeError(f"所有数据源都失败了，本轮无法检索 —— {detail}")

    merged, added = merge_works(groups)
    for report in reports:
        report.added = added.get(report.name, 0)
        if report.status == "ok":
            log.info(
                "[%s] 合并贡献：%s 篇（其中 %s 篇是本轮首次出现）",
                report.label, report.count, report.added,
            )

    if len(merged) > cap:
        log.info(
            "合并后共 %s 篇，超过上限 %s，按发表日期保留最新的 %s 篇",
            len(merged), cap, cap,
        )
        merged = merged[:cap]

    result = FetchResult(works=merged, reports=reports, terms=terms)
    log.info("合并结果：%s 篇 · %s", len(merged), result.summary())
    return result
