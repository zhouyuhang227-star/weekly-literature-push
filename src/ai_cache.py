"""AI 打分缓存：``data/ai_cache.json``。

为什么要缓存
------------
候选里总有一批论文**每周都会被重新打分**：

* 缺摘要、被判低分 —— 按规矩不能标记已读（摘要下周可能出现，结论会反转）；
* 分数在阈值边缘反复横跳；
* 某一轮 AI 调用失败（失败不写已读标记，下轮必重试）。

这些论文的提示词其实一字未变。缓存键 = 主题 + DOI + 提示词指纹 +
**摘要指纹**，所以：

* 摘要还没出现 → 命中缓存，**0 次 AI 调用**；
* 摘要补上了 → 摘要指纹变化 → 自动重新打分（这正是我们想要的）；
* 改了关键词 / 描述 / 模型 / 提示词版本 → 指纹变化 → 全部重新打分。

缓存是**尽力而为**：文件损坏、读不了、写不了都只记 WARNING，绝不影响主流程。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from . import config
from .openalex_client import normalize_doi

log = logging.getLogger(__name__)

CACHE_VERSION = 1


def _path(path: str | None = None) -> str:
    return path or config.AI_CACHE_FILE


def _brief(topic, keywords) -> str:
    if keywords:
        return " | ".join(str(word).strip() for word in keywords if str(word).strip())
    if topic is not None:
        return topic.brief()
    return config.research_brief()


def fingerprint(topic=None, keywords: list[str] | None = None) -> str:
    """提示词指纹：主题判据 + 模型 + 提示词版本。

    不含阈值 —— 分数是客观判断，改阈值不该让整库缓存作废。
    """
    raw = "\n".join(
        [
            f"v={config.PROMPT_VERSION}",
            f"model={config.AI_MODEL}",
            _brief(topic, keywords),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def abstract_signature(work: dict) -> str:
    """摘要指纹。空摘要返回固定串（表示「只有标题」）。"""
    text = (work.get("abstract") or "").strip()
    if not text:
        return "none"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def entry_key(topic, work: dict) -> str:
    """缓存键：``主题key|doi``。"""
    topic_key = getattr(topic, "key", "") or config.RESEARCH_FIELD
    doi = normalize_doi(work.get("doi"))
    return f"{topic_key}|{doi or work.get('title') or ''}"


def _empty() -> dict:
    return {"version": CACHE_VERSION, "entries": {}}


def load(path: str | None = None) -> dict:
    """读取缓存。任何异常都退化为空缓存（绝不拖垮主流程）。"""
    target = _path(path)
    if not os.path.exists(target):
        return _empty()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("AI 缓存读不了（%s），本轮按无缓存运行：%s", target, exc)
        return _empty()

    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        log.warning("AI 缓存格式不对（%s），本轮按无缓存运行", target)
        return _empty()
    return data


def get(cache: dict | None, key: str, prompt_fp: str, abs_sig: str) -> dict | None:
    """命中返回结果字典，否则 ``None``。"""
    if not cache or not config.AI_CACHE_ENABLED:
        return None
    entry = (cache.get("entries") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    if entry.get("fingerprint") != prompt_fp or entry.get("abstract") != abs_sig:
        return None
    max_age = max(0, int(config.AI_CACHE_MAX_AGE_DAYS)) * 86400
    if max_age and time.time() - float(entry.get("ts") or 0) > max_age:
        return None
    return entry


def put(
    cache: dict | None,
    key: str,
    prompt_fp: str,
    abs_sig: str,
    result,
) -> None:
    """把一次成功的打分写进缓存。

    ``result`` 是 ``(score, takeaway, reason, anode_free, cathode_system)``。
    """
    if not cache or not config.AI_CACHE_ENABLED:
        return
    score, takeaway, reason, anode_free, cathode_system = result
    cache.setdefault("version", CACHE_VERSION)
    cache.setdefault("entries", {})[key] = {
        "fingerprint": prompt_fp,
        "abstract": abs_sig,
        "score": int(score),
        "takeaway": takeaway,
        "reason": reason,
        "anode_free": bool(anode_free),
        "cathode_system": cathode_system,
        "ts": time.time(),
    }


def prune(cache: dict | None) -> dict:
    """按时间与条数清理，避免文件无限膨胀。"""
    if not cache:
        return _empty()
    entries = cache.get("entries") or {}
    max_age = max(0, int(config.AI_CACHE_MAX_AGE_DAYS)) * 86400
    now = time.time()
    kept = {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict)
        and (not max_age or now - float(value.get("ts") or 0) <= max_age)
    }
    limit = max(0, int(config.AI_CACHE_MAX_ENTRIES))
    if limit and len(kept) > limit:
        ordered = sorted(kept.items(), key=lambda kv: float(kv[1].get("ts") or 0), reverse=True)
        kept = dict(ordered[:limit])
    return {"version": CACHE_VERSION, "entries": kept}


def save(cache: dict | None, path: str | None = None) -> None:
    """原子写入。失败只记 WARNING；缓存丢了不影响正确性，只是下次重花一次钱。"""
    if not cache:
        return
    target = _path(path)
    payload = prune(cache)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("AI 缓存写入失败（%s）：%s", target, exc)
        return
    log.info("AI 缓存已写入：%s（%s 条）", target, len(payload["entries"]))


def stats(cache: dict | None) -> str:
    """日志用的一行统计。"""
    count = len((cache or {}).get("entries") or {})
    return f"{count} 条"
