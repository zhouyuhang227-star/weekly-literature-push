"""OpenAlex 检索客户端。

职责边界：给一组关键词 + 时间窗，返回结构化文献列表。不做 AI、不做去重。

**召回率由 RETRIEVAL_MODE 决定**（这是整条链路里最影响效果的一个开关）：
默认走 ``topics.id`` 语义主题分类，而不是字面关键词。以「固态电池」方向实测
（11 本顶刊 / 近 30 天）：字面关键词只能召回 36 篇，主题分类能召回 194 篇，
而全量是 2069 篇。字面匹配会漏掉「不含关键词原文但确实相关」的论文
（例如 ``Interfacial resistance in garnet-type Li7La3Zr2O12``）。

主题 id **不需要手改**：``config.TOPIC_QUERY`` 是唯一入口，
``resolve_topics()`` 会调 ``/topics`` 接口把它解析成 id 并缓存。
若允许二者脱钩（改了关键词却没改 id），检索会**静默地继续用旧方向**
且不报错 —— 这正是下面 ``active_topics()`` 要消灭的坑。

相比最初骨架修正的关键点
------------------------
1. **检索字段**：原骨架用的 URL 参数 ``search=`` 实测等价于 ``fulltext`` 全文检索
   （OQL: ``fulltext has (...)``），会把 News & Views、评论等捞进来。
   若使用关键词模式，必须写 ``title_and_abstract.search`` 且放进 ``filter=``
   （作为 URL query 参数会 400）。
2. **时间窗**：新增 ``from_publication_date``，否则首次运行会把历史文献全部当新文献群发。
3. **分页**：新增 cursor 分页。原骨架 ``PER_PAGE=50`` 且无翻页，
   导致 ``MAX_WORKS_FETCH=200`` 永远不可能触发（自相矛盾）。
4. **提前 return 的 bug**：原骨架在达到上限时直接 ``return``，
   后面的关键词再也不会被查询。这里改为遍历完所有页、由外层统一截断。
5. **retracted / paratext 过滤**：排除撤稿与前后缀内容。
6. **DOI 规范化**：OpenAlex 返回 ``https://doi.org/10.xxxx/yyy``，需剥前缀 + 转小写。
7. **无 DOI 的文献**：不再静默丢弃，而是记录 WARNING 日志。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, timedelta

import requests

from . import config
from .config import (
    HTTP_TIMEOUT,
    ISSN_FILTER,
    ISSN_TO_NAME,
    MAX_PAGES,
    MAX_WORKS_FETCH,
    OPENALEX_BASE,
    OPENALEX_MAILTO,
    OPENALEX_TOPICS_API,
    PER_PAGE,
    RETRIEVAL_MODE,
    SEARCH_FIELD,
)

log = logging.getLogger(__name__)

#: 只取需要的字段，显著压缩响应体（实测单次查询 body 从数百 KB 降到几十 KB）
SELECT_FIELDS = ",".join(
    [
        "id",
        "doi",
        "display_name",
        "publication_date",
        "cited_by_count",
        "type",
        "primary_location",
        "abstract_inverted_index",
    ]
)

#: 需要重试的 HTTP 状态码（限流与临时性服务端错误）
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


# ---------------------------------------------------------------------------
# DOI / 摘要处理
# ---------------------------------------------------------------------------
def normalize_doi(raw: str | None) -> str:
    """剥离 ``https://doi.org/`` 等前缀并统一小写。

    去重键必须规范化，否则同一篇文献因大小写或前缀差异会被重复推送。
    """
    if not raw:
        return ""
    doi = raw.strip()
    lowered = doi.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    # 有些来源会在末尾带标点
    return doi.strip().rstrip(".,;").lower()


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """把 OpenAlex 的倒排索引还原成明文摘要。

    OpenAlex 存储格式为 ``{"word": [position, ...], ...}``，需按位置排序后拼接。
    """
    if not inverted_index:
        return ""
    pos_to_word: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            pos_to_word[pos] = word
    return " ".join(pos_to_word[i] for i in sorted(pos_to_word))


def _journal_name(source: dict | None) -> str:
    """取期刊名，缺失时用 ISSN 反查配置表兜底。"""
    if source:
        name = source.get("display_name")
        if name:
            return name
        candidates = [source.get("issn_l")] + list(source.get("issn") or [])
        for issn in candidates:
            if issn and issn in ISSN_TO_NAME:
                return ISSN_TO_NAME[issn]
    return "未知期刊"


# ---------------------------------------------------------------------------
# 查询构造
# ---------------------------------------------------------------------------
def build_keyword_query(keywords: list[str]) -> str:
    """构造 ``"A" OR "B"`` 形式的检索串。

    多词短语必须加引号，否则 OpenAlex 会按空格拆词做 AND，语义完全变了。
    """
    phrases = [f'"{kw.strip()}"' for kw in keywords if kw and kw.strip()]
    return " OR ".join(phrases)


# ---------------------------------------------------------------------------
# 语义主题解析
# ---------------------------------------------------------------------------
# 为什么需要它：topic 模式下如果只改 USER_KEYWORDS 而不改主题 id，
# 检索会**静默地继续用旧方向**（比如改成"钙钛矿太阳能电池"却仍在召回电池论文），
# 而且不报任何错。所以这里让主题 id 从 TOPIC_QUERY 自动派生，改一处即可。
def _load_topic_cache() -> dict:
    """读主题缓存。文件不存在或损坏都当空处理，不影响主流程。"""
    try:
        with open(config.TOPICS_CACHE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_topic_cache(query: str, items: list[dict]) -> None:
    """原子写入主题缓存（合并已有内容，不覆盖别的查询）。"""
    path = config.TOPICS_CACHE_FILE
    cache = _load_topic_cache()
    cache[query] = items
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:  # 缓存写不了不算致命
        log.debug("主题缓存写入失败：%s", exc)


def resolve_topics(query: str, limit: int | None = None) -> dict[str, str]:
    """用 OpenAlex ``/topics`` 接口把一句短语查成语义主题 ``{显示名: id}``。

    **保留 API 返回的相关性顺序，不重新排序** —— 这一点很关键：
    ``/topics?search=`` 是按与查询短语的语义相关度排的，第一个就是最贴切的。
    若改成按 ``works_count`` 降序，``"solid-state battery"`` 会被排到
    ``T12646 Inorganic Fluorides``（11 万篇的泛主题）前面去，
    实际只能召回个位数论文，而且方向完全不对。
    ``works_count`` 只用于日志展示，供人判断主题是否过大/过小。

    结果按 ``limit`` 取前若干个（默认 ``config.TOPIC_RESOLVE_LIMIT``），
    并缓存到 ``data/topics_cache.json``（同一个查询只联网一次）。

    联网失败或查不到时返回空字典 —— 调用方会降级，不会抛异常打断整轮。
    """
    query = (query or "").strip()
    if not query:
        return {}
    if limit is None:
        limit = config.TOPIC_RESOLVE_LIMIT

    cached = _load_topic_cache().get(query)
    if isinstance(cached, list) and cached:
        return {item["name"]: item["id"] for item in cached[:limit]}

    params: dict = {"search": query, "per_page": 25}
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO

    try:
        resp = requests.get(OPENALEX_TOPICS_API, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("主题自动解析失败（%r）：%s —— 请检查网络，或在 config.TOPICS 里手工指定", query, exc)
        return {}

    items = [
        {
            "id": (item.get("id") or "").rsplit("/", 1)[-1],
            "name": item.get("display_name") or "",
            "works_count": item.get("works_count") or 0,
        }
        for item in results
        if item.get("id")
    ]
    if not items:
        log.warning("OpenAlex 没找到与 %r 匹配的主题，请换一个更通用的英文短语", query)
        return {}

    _save_topic_cache(query, items)

    picked = items[:limit]
    log.info(
        "主题自动解析：%r → %s",
        query,
        "、".join(f"{entry['id']}（{entry['name']}，{entry['works_count']} 篇）" for entry in picked),
    )
    if len(items) > 1:
        # 把落选者也打出来，方便人工核对自动选择是否正确
        log.info(
            "  （其余候选，如需手工锁定请用 --find-topic：%s）",
            "；".join(f"{entry['id']}={entry['name']}" for entry in items[1:4]),
        )
    return {entry["name"]: entry["id"] for entry in picked}


def active_topics() -> dict[str, str]:
    """当前生效的主题表。

    优先用 ``config.TOPICS``（手工锁定）；它是空字典时按 ``config.TOPIC_QUERY``
    自动解析。注意读的是 **当前** 的 ``config``，保证「改 config 就生效」。
    """
    if config.TOPICS:
        return dict(config.TOPICS)
    return resolve_topics(config.TOPIC_QUERY)


def topic_filter_value() -> str:
    """``topics.id`` 过滤器要用的 ``T1|T2`` 串；无主题时返回空串。"""
    return "|".join(active_topics().values())


def build_filter(
    from_date: date,
    keywords: list[str] | None = None,
    mode: str = RETRIEVAL_MODE,
) -> str:
    """构造 ``filter=`` 参数。

    检索模式（``mode``）：
      * ``"topic"``   —— 只用 OpenAlex 语义主题分类（``topics.id``）
      * ``"keyword"`` —— 只用字面关键词（``title_and_abstract.search``）
      * ``"both"``    —— 两者并集

    ⚠️ ``title_and_abstract.search`` 必须写进 filter，不能作为 URL query 参数（会 400）。
    检索串本身不含逗号，因此与其它条件用逗号拼接是安全的。
    """
    parts = [
        f"primary_location.source.issn:{ISSN_FILTER}",
        f"from_publication_date:{from_date.isoformat()}",
        "type:article",
        "is_retracted:false",
        "is_paratext:false",
    ]
    if mode in ("topic", "both"):
        topic_ids = topic_filter_value()
        if topic_ids:
            parts.append(f"topics.id:{topic_ids}")
        else:
            log.warning("topic 模式但没解析到任何主题 id，本轮将退化为无主题过滤（召回会暴涨）")
    if mode in ("keyword", "both") and keywords:
        query = build_keyword_query(keywords)
        if query:
            parts.append(f"{SEARCH_FIELD}:{query}")
    return ",".join(parts)


def _describe_filter(mode: str, keywords: list[str] | None) -> str:
    """给人看的检索条件说明，用于日志。"""
    topics = active_topics()
    topic_desc = f"{list(topics.values())}（{'、'.join(topics)}）" if topics else "（未解析到主题）"
    if mode == "topic":
        return f"主题 {topic_desc}"
    if mode == "keyword":
        return f"关键词 {keywords}"
    if mode == "both":
        return f"主题 {topic_desc} + 关键词 {keywords}"
    return f"未知模式 {mode!r}"


def _request_page(params: dict, attempt: int) -> dict:
    """带指数退避的单页请求。"""
    last_exc: Exception | None = None
    for tries in range(1, attempt + 1):
        try:
            resp = requests.get(OPENALEX_BASE, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code in _RETRY_STATUS:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if tries < attempt:
                backoff = 2**tries
                log.warning("OpenAlex 请求失败（%s），%s 秒后重试 %s/%s", exc, backoff, tries, attempt)
                time.sleep(backoff)
    raise RuntimeError(f"OpenAlex 请求最终失败: {last_exc}")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def parse_work(work: dict) -> dict | None:
    """把 OpenAlex 原始记录转成内部结构。无 DOI 时返回 ``None``（并记日志）。"""
    doi = normalize_doi(work.get("doi"))
    if not doi:
        log.warning(
            "跳过无 DOI 文献：openalex_id=%s title=%r",
            work.get("id"),
            (work.get("display_name") or "")[:80],
        )
        return None

    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}",
        "title": (work.get("display_name") or "").strip() or "（无标题）",
        "journal": _journal_name(source),
        "pub_date": work.get("publication_date") or "",
        "cited_by": work.get("cited_by_count") or 0,
        "type": work.get("type") or "",
        "openalex_id": work.get("id") or "",
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
    }


def fetch_works(
    keywords: list[str],
    lookback_days: int,
    max_works: int = MAX_WORKS_FETCH,
    mode: str = RETRIEVAL_MODE,
) -> list[dict]:
    """检索指定时间窗内的顶刊论文，返回去重后的候选列表。

    第 1 层筛选由 ``mode`` 决定（主题分类 / 字面关键词 / 两者并集）。
    所有条件在**一次**查询里组合（比多次查询更省配额、更快）。
    """
    from_date = date.today() - timedelta(days=lookback_days)
    filter_value = build_filter(from_date, keywords, mode)

    log.info(
        "OpenAlex 检索：期刊 %s 本 / %s / 起始日期 %s / 上限 %s 篇",
        len(ISSN_FILTER.split("|")),
        _describe_filter(mode, keywords),
        from_date.isoformat(),
        max_works,
    )
    log.debug("filter=%s", filter_value)

    collected: dict[str, dict] = {}
    cursor = "*"
    page = 0
    dropped = 0

    while cursor and len(collected) < max_works and page < MAX_PAGES:
        page += 1
        remaining = max_works - len(collected)
        params = {
            "filter": filter_value,
            "sort": "publication_date:desc",
            "per-page": max(1, min(PER_PAGE, remaining)),
            "cursor": cursor,
            "select": SELECT_FIELDS,
        }
        if OPENALEX_MAILTO:
            params["mailto"] = OPENALEX_MAILTO

        payload = _request_page(params, attempt=3)
        meta = payload.get("meta") or {}
        results = payload.get("results") or []

        if page == 1:
            oql = (meta.get("x_query") or {}).get("oql", "")
            log.info("OpenAlex 命中总数 meta.count=%s", meta.get("count"))
            if oql:
                # 便于核对检索语义：应当是 title/abstract has，而不是 fulltext has
                log.debug("OpenAlex OQL:\n%s", oql)
                if "fulltext has" in oql:
                    log.warning(
                        "检索落在全文匹配上（SEARCH_FIELD=%s），可能导致大量非研究类命中",
                        SEARCH_FIELD,
                    )

        if not results:
            break

        for raw in results:
            parsed = parse_work(raw)
            if parsed is None:
                dropped += 1
                continue
            collected.setdefault(parsed["openalex_id"] or parsed["doi"], parsed)

        cursor = meta.get("next_cursor") or ""

    works = list(collected.values())
    works.sort(key=lambda w: w.get("pub_date") or "", reverse=True)

    log.info(
        "检索完成：共 %s 篇（%s 页，时间窗 %s 天%s）",
        len(works),
        page,
        lookback_days,
        "" if not dropped else f"，另丢弃 {dropped} 篇无 DOI 记录",
    )
    return works
