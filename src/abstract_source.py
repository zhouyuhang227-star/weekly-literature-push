"""摘要三级回退：OpenAlex → Crossref → Semantic Scholar。

为什么需要这个模块
------------------
需求要求邮件包含文献摘要，但实测发现：**最新顶刊论文的
``abstract_inverted_index`` 普遍为 null**（Nature Energy、Joule 的
2026 年新论文实测全部为 null）。Springer Nature / Wiley / Cell Press
普遍不向 OpenAlex 提供摘要，因此单靠 OpenAlex 无法满足需求。

回退顺序
--------
1. OpenAlex  ``abstract_inverted_index``（已在检索阶段还原）
2. Crossref  ``message.abstract``（Elsevier/Cell 系通常有，是 JATS 富文本，需剥标签）
3. Semantic Scholar ``fields=abstract``（无鉴权约 1 req/s，需串行节流）

三级都拿不到时返回 ``("", "missing")``，由 AI 侧降级为"仅依据标题判断"。

实测结论（2026-09 校验）
-----------------------
* 常规论文：36 篇里 32 篇 OpenAlex 自带摘要，命中率约 89%。
* 刚上线 1–2 周的顶刊论文：OpenAlex 的 ``abstract_inverted_index`` 为 null，
  且 **Crossref 与 Semantic Scholar 同样尚未收录**——
  Semantic Scholar 能查到论文记录（返回 title），但 ``abstract`` 字段为空。
* 结论：这是出版商入库延迟导致的固有缺口，无法靠增加数据源彻底解决。
  代码按约定降级为"摘要暂缺"，并在邮件卡片上标注"依据：标题"以保证透明度。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests

from .config import (
    ABSTRACT_SOURCE_WORKERS,
    CROSSREF_URL,
    HTTP_TIMEOUT,
    OPENALEX_MAILTO,
    S2_CIRCUIT_BREAK_AFTER,
    S2_MAX_RETRIES,
    S2_MIN_INTERVAL,
    S2_URL,
)

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINE_RE = re.compile(r"\n{2,}")

#: Semantic Scholar 的全局节流状态（多线程共享，需加锁）
_s2_lock = threading.Lock()
_s2_last_call = 0.0

#: S2 熔断状态。公共池一旦开始限流，基本会一直限流到本轮结束，
#: 继续逐篇重试只是在烧时间（实测 8 篇要耗掉约 50 秒且一篇都拿不到）。
_s2_state = {"disabled": False, "streak": 0}


def _s2_mark_failure() -> None:
    """记录一次"重试耗尽仍 429"，连续失败到阈值就熔断本轮。"""
    with _s2_lock:
        _s2_state["streak"] += 1
        if _s2_state["streak"] >= S2_CIRCUIT_BREAK_AFTER and not _s2_state["disabled"]:
            _s2_state["disabled"] = True
            log.warning(
                "Semantic Scholar 连续 %s 篇限流，本轮熔断：其余缺失摘要将直接标记为缺失",
                _s2_state["streak"],
            )


def _s2_mark_success() -> None:
    with _s2_lock:
        _s2_state["streak"] = 0



# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------
def clean_text(raw: str | None) -> str:
    """剥离 JATS/HTML 标签、反转义实体、压缩空白。"""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = text.replace("\u2028", " ").replace("\u2029", " ")
    return _WS_RE.sub(" ", text).strip()


def _s2_throttle() -> None:
    """Semantic Scholar 无鉴权限流约 1 req/s，这里做进程内串行节流。"""
    global _s2_last_call
    with _s2_lock:
        elapsed = time.monotonic() - _s2_last_call
        if elapsed < S2_MIN_INTERVAL:
            time.sleep(S2_MIN_INTERVAL - elapsed)
        _s2_last_call = time.monotonic()


# ---------------------------------------------------------------------------
# 各级来源
# ---------------------------------------------------------------------------
def from_crossref(doi: str) -> str:
    params = {"mailto": OPENALEX_MAILTO} if OPENALEX_MAILTO else {}
    try:
        resp = requests.get(
            CROSSREF_URL.format(doi=quote(doi, safe="")),
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log.debug("Crossref 无摘要 %s (HTTP %s)", doi, resp.status_code)
            return ""
        return clean_text((resp.json().get("message") or {}).get("abstract"))
    except (requests.RequestException, ValueError) as exc:
        log.debug("Crossref 请求异常 %s: %s", doi, exc)
        return ""


def from_semantic_scholar(doi: str) -> str:
    """Semantic Scholar Graph API。

    该端点无鉴权时的公共池经常返回 429（实测同一 IP 连续请求即触发，
    稍后重试又能成功），因此这里带指数退避重试，而不是一遇限流就放弃。

    但实测也会出现"整个时段都在限流"的情况：此时逐篇 3 次重试 × 指数退避
    会白白耗掉一分钟。故加了进程级熔断（``S2_CIRCUIT_BREAK_AFTER``）：
    连续若干篇都彻底失败后，本轮剩余文献直接跳过 S2。
    熔断只作用于当前进程，下次运行会自动重试。
    """
    if _s2_state["disabled"]:
        return ""

    url = S2_URL.format(doi=quote(doi, safe=""))
    for attempt in range(1, S2_MAX_RETRIES + 1):
        if _s2_state["disabled"]:
            return ""
        _s2_throttle()
        try:
            resp = requests.get(url, params={"fields": "abstract"}, timeout=HTTP_TIMEOUT)
        except (requests.RequestException, ValueError) as exc:
            log.debug("Semantic Scholar 请求异常 %s: %s", doi, exc)
            return ""

        if resp.status_code == 200:
            _s2_mark_success()
            return clean_text((resp.json() or {}).get("abstract"))

        if resp.status_code == 429:
            if attempt < S2_MAX_RETRIES:
                wait = _retry_after(resp) or 2**attempt
                log.debug("Semantic Scholar 限流，%s 秒后重试 %s/%s：%s", wait, attempt, S2_MAX_RETRIES, doi)
                time.sleep(wait)
                continue
            log.debug("Semantic Scholar 重试耗尽仍限流，放弃：%s", doi)
            _s2_mark_failure()
            return ""

        if resp.status_code == 404:
            _s2_mark_success()  # 论文确实不存在，不是限流
            log.debug("Semantic Scholar 无此论文：%s", doi)
            return ""

        log.debug("Semantic Scholar 异常状态 %s：%s", resp.status_code, doi)
        return ""
    return ""


def _retry_after(resp: requests.Response) -> float | None:
    """解析 Retry-After 响应头（秒）。"""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1.0, min(30.0, float(raw)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def fetch_abstract(work: dict) -> tuple[str, str]:
    """为单篇文献获取摘要。

    :return: ``(摘要正文, 来源标签)``，来源标签为
             ``openalex`` / ``crossref`` / ``semantic_scholar`` / ``missing``
    """
    existing = (work.get("abstract") or "").strip()
    if existing:
        return existing, "openalex"

    doi = work.get("doi") or ""
    if not doi:
        return "", "missing"

    text = from_crossref(doi)
    if text:
        return text, "crossref"

    text = from_semantic_scholar(doi)
    if text:
        return text, "semantic_scholar"

    return "", "missing"


def enrich_abstracts(
    works: list[dict],
    workers: int = ABSTRACT_SOURCE_WORKERS,
) -> list[dict]:
    """并发为缺失摘要的文献补齐，并在每篇上写入 ``abstract`` / ``abstract_source``。

    只对**已在 OpenAlex 缺少摘要**的条目发起外部请求，避免浪费配额。
    """
    if not works:
        return works

    need = [w for w in works if not (w.get("abstract") or "").strip()]
    for work in works:
        if (work.get("abstract") or "").strip():
            work["abstract_source"] = "openalex"

    if need:
        log.info("摘要回退：%s 篇缺少 OpenAlex 摘要，开始 Crossref / S2 回退", len(need))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_abstract, work): work for work in need}
            for future in as_completed(futures):
                work = futures[future]
                try:
                    text, source = future.result()
                except Exception as exc:  # 单篇失败不影响整体
                    log.debug("摘要回退异常 %s: %s", work.get("doi"), exc)
                    text, source = "", "missing"
                work["abstract"] = text
                work["abstract_source"] = source

    stats: dict[str, int] = {}
    for work in works:
        source = work.get("abstract_source") or "missing"
        stats[source] = stats.get(source, 0) + 1

    log.info(
        "摘要统计：OpenAlex %s / Crossref %s / Semantic Scholar %s / 缺失 %s",
        stats.get("openalex", 0),
        stats.get("crossref", 0),
        stats.get("semantic_scholar", 0),
        stats.get("missing", 0),
    )
    return works
