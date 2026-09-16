"""DOI 去重与运行状态持久化。

状态文件 ``data/pushed_dois.json`` 既是去重库也是运行元数据。
**v2 起按主题分区**（多主题调研时，同一篇论文可以出现在两个主题的邮件里）：

.. code-block:: json

    {
      "schema_version": 2,
      "topics": {
        "固态电池": ["10.1038/s41560-026-02133-3"],
        "无负极钠离子电池": []
      },
      "last_run": "2026-09-15"
    }

相比最初骨架修正的关键点
------------------------
1. **状态结构升级**：原骨架只存 ``{"dois": [...]}``，无法判断是否首次运行，
   因此无法实现"首次 30 天预热 / 之后 14 天滚动窗口"。
2. **原子写**：先写 ``.tmp`` 再 ``os.replace``，避免 Actions 中途被杀导致文件损坏。
3. **损坏容错**：JSON 解析失败时回退为空状态并记录 ERROR，而不是直接崩溃。
4. **向后兼容**：能读取旧格式（纯数组 / v1 的全局 ``dois``），
   并自动迁移成 v2：旧 DOI 整体归给「当前第一个主题」，**一条都不丢**。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from . import config
from .openalex_client import normalize_doi

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

#: 没有任何主题可用时的兜底分区名
FALLBACK_TOPIC_KEY = "默认"


def _resolve_path(path: str | None = None) -> str:
    """总是取 **当前** 的 ``config.PUSHED_FILE``（而不是导入时快照），
    这样运行期或测试里改 config 能立刻生效。"""
    return path or config.PUSHED_FILE


def _empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "topics": {}, "last_run": None}


def default_topic_key() -> str:
    """未指定主题时用哪个分区：当前第一个主题；一个都没有就用 ``默认``。"""
    try:
        topics = config.active_research_topics()
    except Exception as exc:  # 配置本身坏掉时不该连状态都读不了
        log.warning("读取主题配置失败（%s），状态分区退回 %r", exc, FALLBACK_TOPIC_KEY)
        topics = []
    if topics:
        return topics[0].key or topics[0].name or FALLBACK_TOPIC_KEY
    return FALLBACK_TOPIC_KEY


def _normalize_state(data) -> dict:
    """把任意历史格式归一成 v2 结构。"""
    state = _empty_state()

    # 最早期的格式：直接存一个数组
    if isinstance(data, list):
        data = {"dois": data}

    if not isinstance(data, dict):
        return state

    state["schema_version"] = data.get("schema_version", SCHEMA_VERSION)
    state["last_run"] = data.get("last_run")

    topics = data.get("topics")
    if isinstance(topics, dict) and topics:
        for key, dois in topics.items():
            if not isinstance(key, str) or not key:
                continue
            state["topics"][key] = [d for d in (dois or []) if isinstance(d, str) and d]
        return state

    # v1：一个全局 dois 列表。v1 无法还原它属于哪个主题，
    # 因此整体归给「当前第一个主题」—— 宁可归错也不能丢，丢了就会重复推送。
    legacy = data.get("dois")
    if isinstance(legacy, list):
        dois = [d for d in legacy if isinstance(d, str) and d]
        if dois:
            state["topics"][default_topic_key()] = dois
    return state


def load_state(path: str | None = None) -> dict:
    """读取状态文件（永远返回 v2 结构）。文件不存在或损坏时返回空状态。"""
    target = _resolve_path(path)
    if not os.path.exists(target):
        log.info("状态文件不存在，按首次运行处理：%s", target)
        return _empty_state()

    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("状态文件损坏（%s），已回退为空状态，本次可能重复推送：%s", target, exc)
        return _empty_state()

    state = _normalize_state(data)
    if state["schema_version"] != SCHEMA_VERSION:
        log.info(
            "状态文件是 v%s（全局 DOI 列表），已自动迁移为 v%s（按主题分区）："
            "旧记录整体归入主题分区 %r",
            state["schema_version"],
            SCHEMA_VERSION,
            default_topic_key(),
        )
        state["schema_version"] = SCHEMA_VERSION
    return state


def topic_counts(path: str | None = None) -> dict[str, int]:
    """各主题分区里记录了多少条（用于日志与"孤儿分区"提醒）。"""
    return {key: len(dois) for key, dois in load_state(path)["topics"].items()}


def orphan_topic_keys(active_keys, path: str | None = None) -> list[str]:
    """找出状态文件里存在、但当前主题列表里已经没有的分区名。

    典型场景：改了 topic 的 ``name``（分区名跟着变），旧分区就成了孤儿。
    不报错、不丢数据，但**旧记录不再生效** → 该主题会重新走首次运行的 30 天预热。
    """
    known = set(load_state(path)["topics"])
    return sorted(known - set(active_keys))


def load_pushed(topic_key: str | None = None, path: str | None = None) -> set[str]:
    """返回已推送的规范化 DOI 集合。

    ``topic_key`` 为空时返回**所有主题的并集**（"这篇到底推过没有"）。
    """
    state = load_state(path)
    if topic_key is None:
        raw = [doi for dois in state["topics"].values() for doi in dois]
    else:
        raw = state["topics"].get(topic_key, [])
    return {normalize_doi(doi) for doi in raw if normalize_doi(doi)}


def is_first_run(topic_key: str | None = None, path: str | None = None) -> bool:
    """该主题是否首次运行（尚无任何已推送记录）。决定使用哪个时间窗。"""
    return not load_pushed(topic_key, path)


def work_key(work: dict) -> str:
    """去重键。DOI 已规范化为小写；无 DOI 时回退到 OpenAlex ID。"""
    doi = normalize_doi(work.get("doi"))
    if doi:
        return doi
    return f"openalex:{work.get('openalex_id') or ''}"


def filter_new(
    works: list[dict],
    pushed: set[str] | None = None,
    topic_key: str | None = None,
    path: str | None = None,
) -> list[dict]:
    """过滤掉该主题已推送的文献，保留新文献（保持原有顺序）。"""
    if pushed is None:
        pushed = load_pushed(topic_key, path)

    fresh: list[dict] = []
    skipped: list[str] = []
    for work in works:
        key = work_key(work)
        if key and key in pushed:
            skipped.append(key)
            continue
        fresh.append(work)

    label = f"主题「{topic_key}」" if topic_key else "全部主题"
    log.info(
        "去重（%s）：%s 篇候选 → 保留 %s 篇新文献（跳过 %s 篇已推送）",
        label,
        len(works),
        len(fresh),
        len(skipped),
    )
    if skipped:
        log.debug("已推送而跳过的 DOI：%s", skipped)
    return fresh


def _atomic_write(payload: dict, path: str | None = None) -> None:
    target = _resolve_path(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp_path = f"{target}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp_path, target)


def save_state(
    new_keys,
    run_date: str | None = None,
    dry_run: bool = False,
    topic_key: str | None = None,
    path: str | None = None,
) -> dict:
    """把新推送的键合并进**指定主题分区**。

    :param new_keys: 可迭代的 DOI / 去重键（生成器也可以）
    :param topic_key: 状态分区名；为空时用当前第一个主题
    :param dry_run: 为 True 时只返回将要写入的内容，不落盘
                    （保证 ``--dry-run`` 完全无副作用）。
    """
    target = _resolve_path(path)
    key = topic_key or default_topic_key()

    # 必须先物化：new_keys 可能是生成器，只能遍历一次
    normalized = sorted({normalize_doi(k) for k in new_keys if k})

    state = load_state(target)
    merged = sorted(load_pushed(key, target) | set(normalized))
    state["topics"][key] = merged

    payload = {
        "schema_version": SCHEMA_VERSION,
        "topics": state["topics"],
        "last_run": run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    if dry_run:
        log.info(
            "[dry-run] 跳过状态写入（主题「%s」本次将新增 %s 个 DOI，累计 %s 个，全文共 %s 个主题）",
            key,
            len(normalized),
            len(merged),
            len(state["topics"]),
        )
        return payload

    _atomic_write(payload, target)
    log.info(
        "状态已写入：%s（主题「%s」新增 %s 个，累计 %s 个，last_run=%s）",
        target,
        key,
        len(normalized),
        len(merged),
        payload["last_run"],
    )
    return payload


def mark_pushed(
    works: list[dict],
    run_date: str | None = None,
    dry_run: bool = False,
    topic_key: str | None = None,
    path: str | None = None,
) -> dict:
    """把一批文献标记为已推送。"""
    return save_state(
        (work_key(w) for w in works),
        run_date=run_date,
        dry_run=dry_run,
        topic_key=topic_key,
        path=path,
    )
