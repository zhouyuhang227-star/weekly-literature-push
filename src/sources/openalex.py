"""OpenAlex 数据源适配器。

**这只是一层薄包装**：真正的检索逻辑仍在 ``src/openalex_client.py`` 里，
本文件只负责把它的输出接进统一接口（补上 ``uid`` / ``sources`` 两个字段），
因此对既有行为**零改动、零回归**。

OpenAlex 为什么是主力源：它是三个源里唯一支持「标题 + 摘要」**短语级**检索的
（``filter=title_and_abstract.search:"li-rich"``）。另外两个源都只能搜标题
（Crossref）或者做布尔匹配（Semantic Scholar），召回质量差一档。

它的短板是 2026 年起按请求计费，匿名额度（1000 积分/天）用光就一律 429，
所以必须有备用源 —— 见 ``src/sources/__init__.py``。
"""

from __future__ import annotations

import logging

from .. import config, openalex_client
from .base import OPENALEX

log = logging.getLogger(__name__)


def fetch(
    keywords: list[str] | None,
    lookback_days: int,
    max_works: int | None = None,
    *,
    mode: str | None = None,
    topic=None,
    **_ignored,
) -> list[dict]:
    """调用 ``openalex_client.fetch_works`` 并补齐多源所需字段。

    ``mode`` / ``topic`` 只有 OpenAlex 支持（语义主题检索是它的独有能力），
    其它源拿到这两个参数也只能忽略。
    """
    works = openalex_client.fetch_works(
        keywords,
        lookback_days,
        max_works=int(max_works or config.MAX_WORKS_FETCH),
        mode=mode or config.RETRIEVAL_MODE,
        topic=topic,
    )

    for work in works:
        # ``uid`` 是跨源统一去重键；OpenAlex 侧原本用 openalex_id，这里补齐。
        work.setdefault("uid", f"doi:{work.get('doi') or ''}")
        work.setdefault("sources", [OPENALEX])
        if work.get("abstract") and not work.get("abstract_from"):
            work["abstract_from"] = OPENALEX
    return works
