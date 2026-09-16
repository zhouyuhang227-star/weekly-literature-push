"""OpenAlex 检索客户端。

职责边界：给一组关键词 + 时间窗，返回结构化文献列表。不做 AI、不做去重。

**召回率由 RETRIEVAL_MODE 决定**（这是整条链路里最影响效果的一个开关）：

* ``keyword``（**默认**）—— 用主题自己的 ``search_terms`` 走
  ``title_and_abstract.search`` 字面召回。实测：「富锂锰正极」用 7 个短词
  OR 并联能召回 18 篇/30 天，「钠离子正极」43 篇/30 天。
* ``topic`` —— OpenAlex 的语义主题分类（``topics.id``）。
  ⚠️ **粒度很粗，别用来做细分方向**：实测 ``"sodium-ion battery"``、
  ``"lithium-rich"`` 这类短语在 ``/topics`` 里一律返回 0 条（根本没有这样的主题），
  只有 ``"solid-state battery"`` 这种大方向才解析得到。
  一旦解析为 0，``build_filter`` 会**直接报错**（见下）。
* ``both`` —— 两者并集。

⚠️ **静默退化是这条链路上最贵的 bug**：早期版本在解析不到主题 id 时只记一条
WARNING 就去掉 ``topics.id`` 条件，查询于是退化成「14 本刊近 30 天」共 3772 篇，
被 ``MAX_WORKS_FETCH`` 截断后两个主题拿到**同一批** 275 篇无关论文
（AI 几乎全部拒绝 → 一个主题报 0 篇、一个报 4 篇，还看起来像「规则太严」）。
现在 ``build_filter`` 在召回条件缺失时抛 ``RuntimeError``：宁可贵主题失败，
也不要「绿着跑错」。

相比最初骨架修正的关键点
------------------------
1. **检索字段**：原骨架用的 URL 参数 ``search=`` 实测等价于 ``fulltext`` 全文检索
   （OQL: ``fulltext has (...)``），会把 News & Views、评论等捞进来。
   用关键词模式时必须写 ``title_and_abstract.search`` 且放进 ``filter=``
   （作为 URL query 参数会 400）。
2. **时间窗**：新增 ``from_publication_date``，否则首次运行会把历史文献全部当新文献群发。
3. **分页**：新增 cursor 分页。原骨架 ``PER_PAGE=50`` 且无翻页，
   导致 ``MAX_WORKS_FETCH=200`` 永远不可能触发（自相矛盾）。
4. **提前 return 的 bug**：原骨架在达到上限时直接 ``return``，
   后面的关键词再也不会被查询。这里改为遍历完所有页、由外层统一截断。
5. **retracted / paratext 过滤**：排除撤稿与前后缀内容。
6. **DOI 规范化**：OpenAlex 返回 ``https://doi.org/10.xxxx/yyy``，需剥前缀 + 转小写。
7. **无 DOI 的文献**：不再静默丢弃，而是记录 WARNING 日志。
8. **额度耗尽**：2026 年起 OpenAlex 按积分收费（匿名 1000 积分/天、
   约 10 积分/次），用完返回 429 ``Insufficient budget`` 且当天不再恢复 ——
   这种 429 会被识别出来并直接给出可操作的解释，不做无意义的重试。
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
        "authorships",
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


def _pick_issn(source: dict | None) -> str:
    """挑一个**能对上配置表**的 ISSN，供期刊档次加权用。

    OpenAlex 的 ``source.issn`` 同时含印刷版与电子版，而配置里写的是 eISSN。
    所以优先返回能在 ``ISSN_TO_NAME`` 里查到的那一个，查不到再退回 ``issn_l``。
    否则会出现「明明配置了这本刊，加成却是 0」的静默错配。
    """
    if not source:
        return ""
    candidates = list(source.get("issn") or []) + [source.get("issn_l")]
    for issn in candidates:
        if issn and issn in ISSN_TO_NAME:
            return str(issn).lower()
    for issn in candidates:
        if issn:
            return str(issn).lower()
    return ""


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

    查不到时返回空字典 —— 但**不代表调用方可以当无事发生**：
    ``build_filter`` 在 topic / both 模式下拿到空字典会直接抛 ``RuntimeError``，
    把该主题当失败处理（否则就会静默变成「全部期刊近 N 天」的全库检索）。
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
        resp = requests.get(
            OPENALEX_TOPICS_API, params=_with_auth(params), timeout=HTTP_TIMEOUT
        )
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
        log.error(
            "OpenAlex 里没有与 %r 匹配的主题（返回 0 条）：这个短语太窄/太长，"
            "主题粒度到不了这么细。请改用更通用的英文短语（用 --find-topic 试），"
            '或把 RETRIEVAL_MODE 改成 "keyword" 并用 search_terms 写字面短词。',
            query,
        )
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


def active_topics(topic=None) -> dict[str, str]:
    """当前生效的主题表。

    传入 ``config.ResearchTopic`` 时用该主题自己的 ``topics`` / ``topic_query``
    （多主题调研时每个主题各查各的）。不传时退回单方向模式：
    优先用 ``config.TOPICS``（手工锁定）；它是空字典时按 ``config.TOPIC_QUERY``
    自动解析。注意读的是 **当前** 的 ``config``，保证「改 config 就生效」。
    """
    if topic is not None:
        if topic.topics:
            return dict(topic.topics)
        return resolve_topics(topic.topic_query)
    if config.TOPICS:
        return dict(config.TOPICS)
    return resolve_topics(config.TOPIC_QUERY)


def topic_filter_value(topic=None) -> str:
    """``topics.id`` 过滤器要用的 ``T1|T2`` 串；无主题时返回空串。"""
    return "|".join(active_topics(topic).values())


def effective_keywords(keywords: list[str] | None, topic=None) -> list[str]:
    """keyword / both 模式下真正用于召回的词表。

    优先级：``--keywords``（命令行） > 主题自己的 ``search_terms`` >
    主题自己的 ``keywords`` > ``config.USER_KEYWORDS``。

    ``search_terms`` 是【召回用的短词】（要求能在标题/摘要里逐字出现），
    ``keywords`` 是【给 AI 看的语义线索】（可以很长）。旧配置没写 ``search_terms``
    时自动退回 ``keywords``，行为与以前一致。

    ⚠️ 这里必须兜底：否则 ``both`` 模式在没传 ``--keywords`` 时会**静默退化成
    纯 topic 模式**，说是并集实际只走主题，现象是「改了关键词也不涨候选量」。
    """
    if keywords:
        return [kw for kw in keywords if kw and kw.strip()]
    if topic is not None:
        return list(topic.search_terms) or list(topic.keywords)
    return list(config.USER_KEYWORDS)


def _topic_label(topic) -> str:
    """报错信息里的主题名（多主题时才有 topic 对象）。"""
    return getattr(topic, "name", "") or config.RESEARCH_FIELD


def build_filter(
    from_date: date,
    keywords: list[str] | None = None,
    mode: str = RETRIEVAL_MODE,
    topic=None,
) -> str:
    """构造 ``filter=`` 参数。

    检索模式（``mode``）：
      * ``"topic"``   —— 只用 OpenAlex 语义主题分类（``topics.id``）
      * ``"keyword"`` —— 只用字面关键词（``title_and_abstract.search``）
      * ``"both"``    —— 两者并集

    ``topic`` 为空时用单方向模式的全局配置（向后兼容）。

    ⚠️ ``title_and_abstract.search`` 必须写进 filter，不能作为 URL query 参数（会 400）。
    检索串本身不含逗号，因此与其它条件用逗号拼接是安全的。

    ⚠️ **召回条件缺失时直接抛 ``RuntimeError``，绝不静默退化成「无过滤」**：
    少一个召回条件，搜索结果就从「你关心的方向」变成「全部期刊近 N 天」，
    而整轮跑完还是绿的、还会发信、还会把 DOI 记进去重库 —— 这种「看起来成功的错」
    比直接失败坏得多（真实事故：topic_query 写得太长解析为 0 个主题，
    两个主题各拿到同一批 300 篇无关论文，一个报 0 篇、一个报 4 篇）。
    """
    parts = [
        f"primary_location.source.issn:{ISSN_FILTER}",
        f"from_publication_date:{from_date.isoformat()}",
        "type:article",
        "is_retracted:false",
        "is_paratext:false",
    ]
    if mode in ("topic", "both"):
        topic_ids = topic_filter_value(topic)
        if topic_ids:
            parts.append(f"topics.id:{topic_ids}")
        elif mode == "topic":
            raise RuntimeError(
                f"topic 模式但主题「{_topic_label(topic)}」没解析到任何 OpenAlex 主题 id"
                "（多半是 topic_query 这个短语在 /topics 里查不到东西 —— "
                "OpenAlex 的主题粒度很粗，短语一长就返回 0 条）。"
                "本轮已中止该主题，以免静默退化成「全部期刊近 N 天」的全库检索。\n"
                "  修法一：`python -m src.main --find-topic \"简短通用的英文短语\"` "
                "试出能解析的短语（日志里能看到 Txxxxx 才算数）；\n"
                '  修法二：把 RETRIEVAL_MODE 改成 "keyword"，并在该主题的 '
                "search_terms 里写能在标题/摘要里逐字出现的短词。"
            )
        else:
            log.warning(
                "both 模式但没解析到主题 id（%s）：本轮只走字面关键词召回", _topic_label(topic)
            )
    if mode in ("keyword", "both"):
        words = effective_keywords(keywords, topic)
        query = build_keyword_query(words)
        if query:
            parts.append(f"{SEARCH_FIELD}:{query}")
        elif mode == "keyword":
            raise RuntimeError(
                f"keyword 模式但主题「{_topic_label(topic)}」的 search_terms / keywords 都是空的："
                "第 1 层没有任何召回条件。本轮已中止该主题，"
                "以免静默退化成「全部期刊近 N 天」的全库检索。\n"
                "  修法：在该主题的 search_terms 里加几条短词（多条之间是 OR）。"
            )
        else:
            log.warning("both 模式但字面词表为空（%s）：本轮只走主题召回", _topic_label(topic))
    return ",".join(parts)


def _describe_filter(mode: str, keywords: list[str] | None, topic=None) -> str:
    """给人看的检索条件说明，用于日志。

    ⚠️ keyword 模式下**不要去解析主题**：那是一次多余的联网请求，
    而且解析失败还会打出误导性的 WARNING（明明跟本轮召回无关）。
    """
    words = effective_keywords(keywords, topic)
    if mode == "keyword":
        return f"字面词组 {len(words)} 个：{'、'.join(words)}"
    topic_desc = ""
    if mode in ("topic", "both"):
        topics = active_topics(topic)
        topic_desc = f"{list(topics.values())}（{'、'.join(topics)}）" if topics else "（未解析到主题）"
    if mode == "both":
        return f"主题 {topic_desc} + 字面词组 {words}"
    if mode == "topic":
        return f"主题 {topic_desc}"
    return f"未知模式 {mode!r}"


def _with_auth(params: dict) -> dict:
    """带上可选的 API key。

    2026 年起 OpenAlex 改成「额度/积分」计费：匿名请求每天 1000 积分、
    每次查询约 10 积分（≈100 次），用完就一直是 429 + ``Insufficient budget``，
    要等到次日 UTC 0 点才恢复。配了 ``OPENALEX_API_KEY`` 就走独立额度，
    **不配则行为与以前完全一样**（一周几次运行的量绰绰有余）。
    """
    if config.OPENALEX_API_KEY:
        return {**params, "api_key": config.OPENALEX_API_KEY}
    return params


def _short_body(resp) -> str:
    """出错响应的人类可读摘要（OpenAlex 的 429 正文是个 JSON）。"""
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or "")[:300]
    return str(payload)[:200]


def _retry_after_seconds(resp) -> int | None:
    raw = (resp.headers.get("Retry-After") or "").strip()
    return int(raw) if raw.isdigit() else None


def _is_budget_exhausted(resp) -> bool:
    """区分「突发限流（等几秒就好）」与「当日额度用完（等也没用）」。"""
    if (resp.headers.get("X-RateLimit-Remaining") or "").strip() == "0":
        return True
    return "budget" in _short_body(resp).lower()


def _budget_message(resp) -> str:
    """429 + 额度耗尽时说清楚原因 —— 否则很容易被当成「网络抽风」反复重试。"""
    seconds = _retry_after_seconds(resp)
    wait = (
        f"约 {seconds / 3600:.1f} 小时后（UTC 零点）" if seconds and seconds > 300 else "稍后"
    )
    quota = resp.headers.get("X-RateLimit-Limit") or "?"
    return (
        f"OpenAlex 今日额度已用完（HTTP 429：{_short_body(resp) or 'Insufficient budget'}）。\n"
        f"  匿名配额 {quota} 积分/天、每次查询约 10 积分，用完后要等{wait}才恢复。\n"
        "  这不是配置错误也不是网络问题，重试没有意义，所以直接停在这里。\n"
        "  彻底解决：注册 OpenAlex 账号，把 key 配成 secret OPENALEX_API_KEY"
        "（本地则设同名环境变量），即可切到独立额度。\n"
        "  临时办法：换个时间再跑 —— GitHub Actions 走的是另一套出口 IP，"
        "一般不受你本机当天额度的拖累。"
    )


def _request_page(params: dict, attempt: int) -> dict:
    """带指数退避的单页请求。

    429 要分两种对待：
      * 突发限流（几秒到一分钟）→ 指数退避重试是有用的；
      * 当日额度用完（``Insufficient budget``，Retry-After 上刀秒）→ 重试毫无意义，
        直接报错并说清原因，免得日志里只留一句干巴巴的 ``HTTP 429``。
    4xx（除 429）通常是查询写错了，重试也没用，同样直接报。
    """
    auth_params = _with_auth(params)
    last_exc: Exception | None = None
    for tries in range(1, attempt + 1):
        try:
            resp = requests.get(OPENALEX_BASE, params=auth_params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:  # 正文不是 JSON，当临时故障重试
                    last_exc = exc
            elif resp.status_code == 429:
                if _is_budget_exhausted(resp):
                    raise RuntimeError(_budget_message(resp))
                last_exc = requests.HTTPError(f"HTTP 429 {_short_body(resp)}")
            elif resp.status_code in _RETRY_STATUS:
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} {_short_body(resp)}")
            elif 400 <= resp.status_code < 500:
                raise RuntimeError(
                    f"OpenAlex 拒绝了查询（HTTP {resp.status_code} {_short_body(resp)}）："
                    "检索条件写错了，重试也不会变好，请检查 filter 里的字段名与语法。\n"
                    f"  filter={params.get('filter')}"
                )
            else:
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}")
        if tries < attempt:
            backoff = 2**tries
            log.warning("OpenAlex 请求失败（%s），%s 秒后重试 %s/%s", last_exc, backoff, tries, attempt)
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
    first_author, corresponding_author = parse_authors(work.get("authorships"))
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}",
        "title": (work.get("display_name") or "").strip() or "（无标题）",
        "journal": _journal_name(source),
        "issn": _pick_issn(source),
        "pub_date": work.get("publication_date") or "",
        "cited_by": work.get("cited_by_count") or 0,
        "type": work.get("type") or "",
        "openalex_id": work.get("id") or "",
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        # 只有 OpenAlex 提供 is_corresponding，所以通讯作者只能从这里得到
        "first_author": first_author,
        "corresponding_author": corresponding_author,
    }


def parse_authors(authorships) -> tuple[str, str]:
    """从 ``authorships`` 抽出 ``(第一作者, 第一通讯作者)``，拿不到就是空串。

    OpenAlex 的结构是 ``authorships: [{author: {display_name}, is_corresponding,
    author_position: "first"/"middle"/"last"}, ...]``。注意两点：

    * **不要用 ``author_position`` 取通讯**：通讯作者跟署名位置无关。
      只看 ``is_corresponding is True``，并按 ``authorships`` 的原始顺序
      取第一个（OpenAlex 的顺序就是署名顺序）。
    * 一作优先用 ``author_position == "first"``（有些记录的顺序字段更可靠），
      没标就退回列表第一条 —— 比直接取 ``[0]`` 稳。

    共同一作（多个 ``is_corresponding``、脚注标共一）在这里**不做区分**：
    元数据里没有共一信息（那是出版社 PDF 脚注里的文字）。
    """
    entries = [entry for entry in (authorships or []) if isinstance(entry, dict)]
    if not entries:
        return "", ""

    def name_of(entry: dict) -> str:
        return author_name((entry or {}).get("author"))

    first = ""
    for entry in entries:
        if str(entry.get("author_position") or "").strip().lower() == "first":
            first = name_of(entry)
            if first:
                break
    if not first:
        for entry in entries:
            first = name_of(entry)
            if first:
                break

    corresponding = ""
    for entry in entries:
        if entry.get("is_corresponding") is True:
            corresponding = name_of(entry)
            if corresponding:
                break
    return first, corresponding


def author_name(author) -> str:
    """作者名：优先 ``display_name``（排版过的写法），退回 ``raw_author_name``。"""
    from . import authors

    if not isinstance(author, dict):
        return authors.clean_name(author)
    return authors.clean_name(author.get("display_name") or author.get("raw_author_name"))



def fetch_works(
    keywords: list[str] | None,
    lookback_days: int,
    max_works: int = MAX_WORKS_FETCH,
    mode: str = RETRIEVAL_MODE,
    topic=None,
) -> list[dict]:
    """检索指定时间窗内的顶刊论文，返回去重后的候选列表。

    第 1 层筛选由 ``mode`` 决定（主题分类 / 字面关键词 / 两者并集）。
    所有条件在**一次**查询里组合（比多次查询更省配额、更快）。
    ``topic`` 为空时用单方向模式的全局配置（向后兼容）。
    """
    from_date = date.today() - timedelta(days=lookback_days)
    filter_value = build_filter(from_date, keywords, mode, topic)

    log.info(
        "OpenAlex 检索：期刊 %s 本 / %s / 起始日期 %s / 上限 %s 篇",
        len(ISSN_FILTER.split("|")),
        _describe_filter(mode, keywords, topic),
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
