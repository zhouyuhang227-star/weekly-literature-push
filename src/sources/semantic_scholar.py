"""Semantic Scholar（S2）数据源适配器。

为什么是它
----------
* **一次请求就能拉回全量候选**：``paper/search/bulk`` 支持 ``limit=1000``，
  实测富锂锰正极 14 个召回词 + 90 天 = 685 条，2.9 秒拉完（含摘要）。
* **摘要覆盖好**：约 80%–94%（Wiley 系刊接近 100%），能给 OpenAlex 缺摘要的论文补摘要。
* 无需密钥，未鉴权可用。

⚠️ 四个必须知道的坑（都是实测出来的，不是文档里的）
---------------------------------------------------
1. **OR 必须用 `` | `` 竖线，空格是 AND。** 实测同一组 14 个词：
   ``"a" | "b" | ...`` → total=685；``"a" "b" ...``（空格分隔）→ **total=0**。
   这个坑极隐蔽 —— 写错了不会报错，只会安静地返回 0 条。
2. **``venue`` 字段和 ``publicationVenue`` 都不可靠。**
   ``10.1002/adma.74958``（货真价实的 Advanced Materials 论文）被标成
   ``venue="Advances in Materials"``、``publicationVenue.issn="2327-2503"``
   （另一本真实存在的期刊的 ISSN）。Angewandte Chemie Int. Ed. 的
   ``publicationVenue`` 则干脆是 ``null``。
   → 所以期刊识别**只信 DOI 前缀**（``config.JOURNAL_DOI_PATTERNS``），
   刊名只作兜底（``config.S2_VENUE_ALIASES``）。见 ``base.identify_journal``。
3. **``venue=`` 查询参数是模糊匹配**，会漏会错（要 ``Small`` 也可能返回别家）。
   所以这里**不传 venue**，改为一次拉全量再本地识别期刊 —— 实测这样拿到的
   论文比传 venue 更多（108 条 vs 47 条）。
4. 未鉴权时限流约 1 请求/秒，且**不返回任何限流响应头**（实测 ``{}``），
   所以只能靠主动节流，不能靠响应头自适应。
"""

from __future__ import annotations

import datetime
import logging
import threading
import time

import requests

from .. import config
from .base import (
    SEMANTIC_SCHOLAR,
    identify_journal,
    make_work,
    matches_recall_terms,
    usable_terms,
)

log = logging.getLogger(__name__)

_BULK_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"

_FIELDS = (
    "title,externalIds,venue,publicationDate,abstract,publicationTypes,authors"
)


def _first_author(item: dict) -> str:
    """S2 作者数组里的第一作者；拿不到返回 ``""``。

    S2 只给 ``{"name": "J. Smith"}`` 这种压过的写法（没有 given/family），
    也没有作者顺序字段，所以就是数组第一条。
    **S2 没有通讯作者字段**，这里不猜。
    """
    from .. import authors

    for entry in item.get("authors") or []:
        if isinstance(entry, dict):
            name = authors.clean_name(entry.get("name"))
        else:
            name = authors.clean_name(entry)
        if name:
            return name
    return ""

#: 主动节流的全局状态（S2 不给我们限流头，只能自己数）
_throttle_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """两次 S2 请求之间至少间隔 ``config.S2_MIN_INTERVAL`` 秒。"""
    global _last_call
    with _throttle_lock:
        wait = float(config.S2_MIN_INTERVAL) - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _headers() -> dict[str, str]:
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def _or_query(terms: list[str]) -> str:
    """S2 的 OR 语法：``"a" | "b"``。**空格等于 AND，写错就是 0 条。**"""
    return " | ".join(f'"{term}"' for term in terms)


def _request(params: dict, attempts: int = 3) -> dict:
    """带节流与退避的一次请求。``429`` 会等待后重试。"""
    last_error = ""
    for attempt in range(1, attempts + 1):
        _throttle()
        try:
            resp = requests.get(
                _BULK_URL, params=params, headers=_headers(),
                timeout=config.HTTP_TIMEOUT * 4,
            )
        except requests.RequestException as exc:
            last_error = f"网络错误 {exc}"
            if attempt < attempts:
                time.sleep(3 * attempt)
                continue
            raise RuntimeError(last_error) from exc

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            last_error = "HTTP 429（限流）"
            if attempt < attempts:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(last_error)
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:160]}")
    raise RuntimeError(last_error or "未知错误")


def _to_work(item: dict) -> tuple[dict | None, str]:
    """转成统一结构。返回 ``(work 或 None, 未能识别的刊名)``。"""
    doi = (item.get("externalIds") or {}).get("DOI")
    venue = item.get("venue") or ""
    journal = identify_journal(
        doi=doi, venue=venue, aliases=config.S2_VENUE_ALIASES
    )
    if not journal:
        return None, venue

    raw_types = item.get("publicationTypes") or []
    work = make_work(
        doi=doi,
        title=item.get("title"),
        source=SEMANTIC_SCHOLAR,
        journal=journal,
        issn=config.JOURNALS.get(journal, ""),
        pub_date=item.get("publicationDate") or "",
        type_=",".join(str(t) for t in raw_types) if raw_types else "",
        abstract=item.get("abstract") or "",
        abstract_from=SEMANTIC_SCHOLAR if item.get("abstract") else "",
        first_author=_first_author(item),
    )
    return work, ""


def fetch(
    keywords: list[str] | None,
    lookback_days: int,
    max_works: int | None = None,
    **_ignored,
) -> list[dict]:
    """一次 bulk 检索拉回候选，再本地做期刊识别 + 关键词复核。"""
    terms = [str(term).strip() for term in (keywords or []) if str(term).strip()]
    if not terms:
        raise RuntimeError(
            "Semantic Scholar 源没有可用的召回词（它只支持字面关键词检索，"
            "不支持语义主题模式）。请给主题保留至少一个 search_terms 词，"
            "或把它从 DATA_SOURCES 里去掉。"
        )

    limit = max(1, min(1000, int(config.S2_BULK_LIMIT)))
    iso_from = (
        datetime.date.today() - datetime.timedelta(days=max(1, int(lookback_days)))
    ).isoformat()
    query = _or_query(terms)

    checkable = usable_terms(terms)
    if not checkable:
        log.warning(
            "Semantic Scholar：召回词全部短于本地复核所需长度，"
            "本轮将对所有命中**不做复核**。建议补一个长一点的 search_terms 词。"
        )

    log.info(
        "Semantic Scholar 检索：起始日期 %s / 单轮上限 %s 条 / OR 词 %s 个",
        iso_from, limit, len(terms),
    )

    params: dict[str, object] = {
        "query": query,
        "publicationDateOrYear": f"{iso_from}:",
        "limit": limit,
        "fields": _FIELDS,
    }
    payload = _request(params)

    total = payload.get("total")
    items = payload.get("data") or []
    log.info("Semantic Scholar 命中总数 total=%s，本次取回 %s 条", total, len(items))
    if isinstance(total, int) and total > len(items):
        log.warning(
            "Semantic Scholar 命中总数（%s）超过单轮上限（%s），只取回了前 %s 条。"
            "若总是如此，说明召回词偏泛，建议收窄 search_terms。",
            total, limit, len(items),
        )

    works: list[dict] = []
    no_doi = 0
    unmatched: dict[str, int] = {}
    dropped = 0

    for item in items:
        work, venue = _to_work(item)
        if work is None:
            if (item.get("externalIds") or {}).get("DOI"):
                unmatched[venue or "（空刊名）"] = unmatched.get(venue or "（空刊名）", 0) + 1
            else:
                no_doi += 1
            continue
        if checkable and not matches_recall_terms(work, terms):
            dropped += 1
            continue
        works.append(work)

    if no_doi:
        log.debug("Semantic Scholar 跳过 %s 条无 DOI 记录", no_doi)
    if dropped:
        log.debug("Semantic Scholar 关键词复核剔除 %s 条", dropped)
    if unmatched:
        # 这条日志很关键：只要出现「无法识别期刊」，就说明有论文被丢掉了。
        # 把刊名列出来，方便往 config.S2_VENUE_ALIASES 或 JOURNAL_DOI_PATTERNS 里补。
        top = sorted(unmatched.items(), key=lambda kv: -kv[1])[:8]
        log.warning(
            "Semantic Scholar 有 %s 条记录无法识别期刊（已丢弃）：%s。"
            "如果里面出现了你要的期刊，请把它的写法加进 config.S2_VENUE_ALIASES，"
            "或把该刊的 DOI 前缀加进 config.JOURNAL_DOI_PATTERNS。",
            sum(unmatched.values()),
            "、".join(f"{name}×{count}" for name, count in top),
        )

    log.info("Semantic Scholar 命中 %s 条（本地识别 + 复核后）", len(works))
    return works
