"""Crossref 数据源适配器。

为什么是它
----------
* **无配额、无需密钥**：Crossref 是 DOI 注册机构本身的接口，请求量再大也不收费，
  正好补 OpenAlex「积分用光就断供」的缺口。
* **刊名归属 100% 准确**：它支持「按 ISSN 查某一本刊的工作」这个端点
  （``/journals/{issn}/works``），不需要猜刊名，也就不会出现 Semantic Scholar
  那种「Advanced Materials 被标成 Advances in Materials」的错误。
* **摘要覆盖高**：实测 Wiley 系刊（AM / AFM / Small / Angew）90 天命中里摘要覆盖
  100%，ACS 系约 88%，Nature 系偏低。摘要是 JATS XML，用
  ``abstract_source.clean_text`` 洗干净即可 —— 所以它不只提供候选，
  还能给 OpenAlex 没摘要的论文补上摘要，直接提高 AI 打分的准确性。

⚠️ 两个必须知道的坑
-------------------
1. ``query.title`` 是**分词匹配**，不是短语匹配。实测 ``query.title=lithium-rich``
   会把 "Cellulosic Composites in Lithium Metal Batteries" 这类一起返回
   （实测 71 条里大量是锂金属/电解液论文）。所以结果必须过
   ``matches_recall_terms`` 本地复核，否则会把一堆噪音喂给 AI（白烧钱），
   甚至写进去重库。实测 15 本刊 90 天：`query.title` 返回 60 条上限的候选里，
   复核后只剩约 70 条真命中（Nature 系甚至 10 条全被复核剔除）。
2. **RSC 系的 Energy & Environmental Science 在 Crossref 里查不到任何记录**：
   实测 eISSN ``1754-5706`` 与 pISSN ``1754-5692`` 的 total 都是 0（RSC 的论文
   没有按 ISSN 关联到 Crossref 的期刊记录里）。该刊只能由 OpenAlex 覆盖，
   这里会记一条 INFO 说明，**不静默**。
3. **有些刊 Crossref 不给摘要**（实测 Joule 0/100 条带 abstract，Nature Energy
   8/100，而 Wiley 系刊 91/100）。对这几本刊，本地复核只能看到标题，反而会误杀
   「标题不含字面词但确实是本主题」的论文 ⇒ 名单里的刊**记录无摘要时不做复核**，
   交由 AI 阈值兜底（``config.CROSSREF_TITLE_ONLY_JOURNALS``）。放宽的条数会记 INFO。
"""

from __future__ import annotations

import datetime
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .. import config
from ..abstract_source import clean_text
from .base import CROSSREF, make_work, matches_recall_terms, usable_terms

log = logging.getLogger(__name__)

#: 逐刊端点。用 ISSN（不是刊名）定位，所以刊名归属不需要猜。
_JOURNAL_URL = "https://api.crossref.org/journals/{issn}/works"

#: 只取需要的字段，响应体能小一大截。
#: ``author`` 里只用到 given/family/sequence，但 select 只能按字段整体开 ——
#: 实测一整刊的 author 数组加不了多少字节，比多打一次请求划算。
_SELECT = (
    "DOI,title,container-title,published,published-print,published-online,"
    "abstract,type,is-referenced-by-count,author"
)

#: 取发表日期时按这个顺序找第一个有值的字段（Crossref 的字段经常缺）
_DATE_KEYS = ("published", "published-online", "published-print", "issued", "created")


def _headers() -> dict[str, str]:
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def _or_query(terms: list[str]) -> str:
    """把召回词拼成 Crossref 的 Lucene OR 串：``"a" OR "b" OR "c"``。

    实测这种写法有效：Angew 90 天里 1 个词命中 24 条、2 个词 66 条、
    14 个词 162 条。
    """
    return " OR ".join(f'"{term}"' for term in terms)


def _pub_date(item: dict) -> str:
    """从 Crossref 的 ``date-parts`` 里取一个 ``YYYY-MM-DD``（缺月日补 1）。"""
    for key in _DATE_KEYS:
        parts = (item.get(key) or {}).get("date-parts") or []
        if not parts or not parts[0]:
            continue
        row = list(parts[0]) + [1, 1]
        try:
            return datetime.date(int(row[0]), int(row[1]), int(row[2])).isoformat()
        except (TypeError, ValueError):
            continue
    return ""


def _first_author(item: dict) -> str:
    """Crossref 作者数组里的第一作者；拿不到返回 ``""``。

    Crossref 给 ``given`` / ``family``，也有的记录只有机构作者（``name``）。
    ``sequence`` 字段有标记时优先信它（值就是 ``"first"``），没标就按数组顺序 ——
    比直接取 ``[0]`` 稳。

    **Crossref 没有通讯作者字段**（那是 OpenAlex 独有的），所以这里只返回一作；
    绝不拿末位作者冒充通讯。
    """
    from .. import authors

    entries = [entry for entry in (item.get("author") or []) if isinstance(entry, dict)]

    def name_of(entry: dict) -> str:
        return authors.join_name(entry.get("given"), entry.get("family"), entry.get("name"))

    for entry in entries:
        if str(entry.get("sequence") or "").strip().lower() == "first":
            name = name_of(entry)
            if name:
                return name
    for entry in entries:
        name = name_of(entry)
        if name:
            return name
    return ""


def _to_work(item: dict, journal: str, issn: str) -> dict | None:
    """把一条 Crossref item 转成统一结构；无 DOI 返回 ``None``。

    ``journal`` 用的是**配置里的刊名**而不是 Crossref 的 ``container-title`` ——
    因为我们是按 ISSN 查的，这一点比 Crossref 返回的字符串更权威，
    而且能保证期刊档次加成（按 ISSN 匹配）稳定生效。
    """
    titles = item.get("title") or []
    return make_work(
        doi=item.get("DOI"),
        title=(titles[0] if titles else ""),
        source=CROSSREF,
        journal=journal,
        issn=issn,
        pub_date=_pub_date(item),
        cited_by=item.get("is-referenced-by-count") or 0,
        type_=item.get("type") or "",
        abstract=clean_text(item.get("abstract")),
        abstract_from=CROSSREF,
        first_author=_first_author(item),
    )


def _request_works(journal: str, issn: str, params: dict[str, object]) -> list[dict]:
    """请求一本刊的 works，带 429 / 5xx 重试。

    加重试的原因很具体：实测 5 并发时 Crossref 会回 429，而那一次的 429
    等于**整本刊从本轮结果里消失**，且对外看起来完全正常（源状态仍是 ok）。
    宁可多等几秒，也不能静默少一本刊。
    """
    attempts = max(1, int(config.CROSSREF_RETRIES))
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(
                _JOURNAL_URL.format(issn=issn),
                params=params,
                headers=_headers(),
                timeout=config.HTTP_TIMEOUT * 2,
            )
        except requests.RequestException as exc:  # 网络抖动也重试
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                return (resp.json().get("message") or {}).get("items") or []
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in config.HTTP_RETRY_STATUS:
                break

        if attempt < attempts:
            wait = max(0.0, float(config.CROSSREF_RETRY_WAIT)) * attempt
            log.debug(
                "Crossref《%s》第 %s/%s 次请求失败（%s），%s 秒后重试",
                journal, attempt, attempts, last_error, wait,
            )
            if wait:
                time.sleep(wait)

    # attempt 在循环结束后仍有值（attempts 至少为 1），用它区分
    # 「重试过还是失败」与「不可重试的错误」（如 404，一次就放弃）。
    if attempt <= 1:
        raise RuntimeError(last_error)
    raise RuntimeError(f"{last_error}（已重试 {attempt - 1} 次）")


def _fetch_one(
    journal: str, issn: str, query: str, iso_from: str, rows: int,
    terms: list[str], checkable: list[str],
) -> tuple[list[dict], int, int, int]:
    """查一本刊。返回 ``(复核通过的 work, 被复核剔除的条数, 原始条数, 放宽的条数)``。"""
    params: dict[str, object] = {
        "filter": f"from-pub-date:{iso_from},type:journal-article",
        "query.title": query,
        "rows": rows,
        "select": _SELECT,
    }
    if config.CROSSREF_MAILTO:
        # 带上邮箱就进 Crossref 的「礼貌池」，速度与稳定性都更好。
        params["mailto"] = config.CROSSREF_MAILTO

    items = _request_works(journal, issn, params)
    # ★ 这几本刊 Crossref 常不给摘要，复核只能看到标题（见 config 里的实测数据）。
    #   标题不含字面词但确实是本主题的论文会被误杀 ⇒ 这种残缺判据不如交给 AI。
    title_only = journal in config.CROSSREF_TITLE_ONLY_JOURNALS

    works: list[dict] = []
    dropped = 0
    relaxed = 0
    for item in items:
        work = _to_work(item, journal, issn)
        if work is None:
            continue
        if checkable and not matches_recall_terms(work, terms):
            # 只有「该刊偶发缺摘要」才放宽；这条恰好有摘要时复核是完整的，照旧剔。
            if title_only and not (work.get("abstract") or "").strip():
                relaxed += 1
            else:
                dropped += 1
                continue
        works.append(work)
    return works, dropped, len(items), relaxed


def fetch(
    keywords: list[str] | None,
    lookback_days: int,
    max_works: int | None = None,
    notes: list[str] | None = None,
    **_ignored,
) -> list[dict]:
    """按 15 本刊逐刊检索并汇总（并发）。

    ``max_works`` 只用于日志提示 —— 真正的总量截断在聚合器里统一做，
    否则各源各截一段会让合并结果难以预测。

    ``notes`` 是**可选的向用户告警通道**：源返回 ``list`` 装不下「源成功了但
    结果不完整」这类信息，而单刊查询失败恰恰属于这类（源状态仍是 ok，
    邮件页头只能看到一个偏小的篇数）。聚合器会把它拼进邮件页头。
    """
    terms = [str(term).strip() for term in (keywords or []) if str(term).strip()]
    if not terms:
        # 继承项目那条最贵的教训：**没有召回条件时必须报错，不能悄悄放宽**。
        # 放宽带在这里就等于「不传 query.title」，会把整本刊近 90 天的论文全捞回来。
        raise RuntimeError(
            "Crossref 源没有可用的召回词（它只支持字面关键词检索，"
            "不支持语义主题模式）。请给主题保留至少一个 search_terms 词，"
            "或把它从 DATA_SOURCES 里去掉。"
        )

    rows = max(1, min(1000, int(config.CROSSREF_ROWS)))
    iso_from = (
        datetime.date.today() - datetime.timedelta(days=max(1, int(lookback_days)))
    ).isoformat()
    query = _or_query(terms)
    journals = list(config.JOURNALS.items())

    # 本地复核能否生效，取决于召回词的长度（见 base.usable_terms）。
    checkable = usable_terms(terms)
    if not checkable:
        log.warning(
            "Crossref：召回词全部短于本地复核所需长度，本轮将对所有命中**不做复核**。"
            "建议给主题补一个长一点的 search_terms 词。"
        )

    log.info(
        "Crossref 检索：%s 本刊 / 起始日期 %s / 每刊上限 %s 条 / OR 词 %s 个 / 并发 %s",
        len(journals), iso_from, rows, len(terms), int(config.CROSSREF_CONCURRENCY),
    )

    works: list[dict] = []
    errors: list[str] = []
    failed_journals: list[str] = []
    empty: list[str] = []
    relaxed_journals: list[str] = []
    relaxed_total = 0

    with ThreadPoolExecutor(
        max_workers=max(1, int(config.CROSSREF_CONCURRENCY))
    ) as pool:
        futures = {
            pool.submit(
                _fetch_one, name, issn, query, iso_from, rows, terms, checkable
            ): name
            for name, issn in journals
        }
        for future in as_completed(futures):
            journal = futures[future]
            try:
                got, dropped, raw, relaxed = future.result()
            except Exception as exc:  # noqa: BLE001 - 单刊失败不该拖垮整轮
                log.warning("Crossref 查询《%s》失败：%s", journal, exc)
                errors.append(f"{journal}（{exc}）")
                failed_journals.append(journal)
                continue
            works.extend(got)
            if raw == 0:
                empty.append(journal)
            if relaxed:
                relaxed_total += relaxed
                relaxed_journals.append(f"{journal} {relaxed} 条")
            log.debug(
                "Crossref《%s》：取回 %s 条 → 复核后 %s 条（剔除 %s 条，放宽 %s 条）",
                journal, raw, len(got), dropped, relaxed,
            )

    if errors and len(errors) == len(journals):
        raise RuntimeError(
            "Crossref 全部 %s 本刊都查询失败：%s" % (len(journals), "；".join(errors[:3]))
        )
    if errors:
        # ★ 这是缺陷 B 的关键：单刊失败时源状态依然是 ok，邮件页头如果只写
        #   「Crossref 27 篇」，用户就看不出**整本刊都缺失**（实测少过 
        #   Angewandte 一本，就差 12 篇）。所以必须把这件事往上抛。
        log.warning("Crossref 有 %s/%s 本刊查询失败：%s",
                    len(errors), len(journals), "、".join(errors))
        if notes is not None:
            notes.append(
                f"有 {len(errors)}/{len(journals)} 本刊查询失败"
                f"（{'、'.join(failed_journals)}），这几本刊本轮的结果缺失，"
                "总量可能比平时少。详见运行日志。"
            )
    if empty:
        log.info(
            "Crossref 以下 %s 本刊在窗口期内没有命中：%s"
            "（若含 Energy & Environmental Science 属正常 —— RSC 的论文未按 ISSN "
            "关联进 Crossref，该刊只能由 OpenAlex 覆盖）",
            len(empty), "、".join(empty),
        )
    if relaxed_total:
        # 放宽复核是**主动降低筛选强度**，必须留痕，否则又成了静默降级。
        log.info(
            "Crossref 有 %s 条命中因「该刊不给摘要、复核只能看标题」而放宽：%s"
            "（这几本刊已在 config.CROSSREF_TITLE_ONLY_JOURNALS 里，交由 AI 阈值兜底）",
            relaxed_total, "、".join(relaxed_journals),
        )

    log.info("Crossref 命中 %s 条（本地复核后）", len(works))
    return works
