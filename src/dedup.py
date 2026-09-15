"""DOI 去重与运行状态持久化。

状态文件 ``data/pushed_dois.json`` 既是去重库也是运行元数据：

.. code-block:: json

    {
      "schema_version": 1,
      "dois": ["10.1038/s41560-026-02133-3"],
      "last_run": "2026-09-15"
    }

相比最初骨架修正的关键点
------------------------
1. **状态结构升级**：原骨架只存 ``{"dois": [...]}``，无法判断是否首次运行，
   因此无法实现"首次 30 天预热 / 之后 14 天滚动窗口"。
2. **原子写**：先写 ``.tmp`` 再 ``os.replace``，避免 Actions 中途被杀导致文件损坏。
3. **损坏容错**：JSON 解析失败时回退为空状态并记录 ERROR，而不是直接崩溃。
4. **向后兼容**：能读取旧格式（纯数组或只有 ``dois`` 键）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from .config import PUSHED_FILE
from .openalex_client import normalize_doi

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "dois": [], "last_run": None}


def load_state() -> dict:
    """读取状态文件。文件不存在或损坏时返回空状态。"""
    if not os.path.exists(PUSHED_FILE):
        log.info("状态文件不存在，按首次运行处理：%s", PUSHED_FILE)
        return _empty_state()

    try:
        with open(PUSHED_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("状态文件损坏（%s），已回退为空状态，本次可能重复推送：%s", PUSHED_FILE, exc)
        return _empty_state()

    # 向后兼容：早期版本可能直接存一个数组
    if isinstance(data, list):
        data = {"dois": data}

    if not isinstance(data, dict):
        log.error("状态文件格式异常，已回退为空状态：%s", PUSHED_FILE)
        return _empty_state()

    state = _empty_state()
    state["schema_version"] = data.get("schema_version", SCHEMA_VERSION)
    state["dois"] = [d for d in (data.get("dois") or []) if isinstance(d, str) and d]
    state["last_run"] = data.get("last_run")
    return state


def load_pushed() -> set[str]:
    """返回已推送的规范化 DOI 集合。"""
    return {normalize_doi(doi) for doi in load_state()["dois"] if normalize_doi(doi)}


def is_first_run() -> bool:
    """是否首次运行（尚无任何已推送记录）。决定使用哪个时间窗。"""
    return not load_state()["dois"]


def work_key(work: dict) -> str:
    """去重键。DOI 已规范化为小写；无 DOI 时回退到 OpenAlex ID。"""
    doi = normalize_doi(work.get("doi"))
    if doi:
        return doi
    return f"openalex:{work.get('openalex_id') or ''}"


def filter_new(works: list[dict], pushed: set[str] | None = None) -> list[dict]:
    """过滤掉已推送的文献，保留新文献（保持原有顺序）。"""
    if pushed is None:
        pushed = load_pushed()

    fresh: list[dict] = []
    skipped: list[str] = []
    for work in works:
        key = work_key(work)
        if key and key in pushed:
            skipped.append(key)
            continue
        fresh.append(work)

    log.info("去重：%s 篇候选 → 保留 %s 篇新文献（跳过 %s 篇已推送）", len(works), len(fresh), len(skipped))
    if skipped:
        log.debug("已推送而跳过的 DOI：%s", skipped)
    return fresh


def _atomic_write(payload: dict) -> None:
    os.makedirs(os.path.dirname(PUSHED_FILE), exist_ok=True)
    tmp_path = f"{PUSHED_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp_path, PUSHED_FILE)


def save_state(new_keys, run_date: str | None = None, dry_run: bool = False) -> dict:
    """把新推送的键合并进状态文件。

    :param new_keys: 可迭代的 DOI / 去重键（生成器也可以）
    :param dry_run: 为 True 时只返回将要写入的内容，不落盘
                    （保证 ``--dry-run`` 完全无副作用）。
    """
    # 必须先物化：new_keys 可能是生成器，只能遍历一次
    normalized = sorted({normalize_doi(k) for k in new_keys if k})

    merged = sorted(set(load_pushed()) | set(normalized))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "dois": merged,
        "last_run": run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    if dry_run:
        log.info("[dry-run] 跳过状态写入（本次将新增 %s 个 DOI，累计 %s 个）", len(normalized), len(merged))
        return payload

    _atomic_write(payload)
    log.info(
        "状态已写入：%s（新增 %s 个，累计 %s 个，last_run=%s）",
        PUSHED_FILE,
        len(normalized),
        len(merged),
        payload["last_run"],
    )
    return payload


def mark_pushed(works: list[dict], run_date: str | None = None, dry_run: bool = False) -> dict:
    """把一批文献标记为已推送。"""
    return save_state((work_key(w) for w in works), run_date=run_date, dry_run=dry_run)
