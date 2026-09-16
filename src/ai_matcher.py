"""AI 相关性打分。

默认端点 DeepSeek（``https://api.deepseek.com/v1``），任何 OpenAI 兼容服务
只需改环境变量 ``AI_BASE_URL`` / ``AI_MODEL``，代码无需改动。

**本文件不含任何学科特定文字**：system prompt、user prompt 里的研究方向、
评分档位说明全部由 ``config.py`` 的「研究方向」段落派生。
换课题只需改 ``RESEARCH_FIELD`` / ``RESEARCH_DESCRIPTION`` / ``USER_KEYWORDS``。

相比最初骨架修正的关键点
------------------------
1. **``int()`` 强转**：原骨架直接把 ``data["score"]`` 塞进结果，
   模型返回字符串 ``"85"`` 时 ``sorted(key=lambda x: -x["ai_score"])`` 直接 TypeError。
2. **并发**：原骨架串行调用，200 篇约需 10–20 分钟且极易超时。这里用线程池。
3. **重试 + 指数退避**：原骨架异常只 ``print`` 后静默丢弃，
   网络抖动会导致整篇文献丢失。
4. **失败可见**：全部重试失败时给保守默认分并标记 ``ai_error``，不再静默丢稿。
5. **健壮 JSON 解析**：先剥 ```json 围栏，再退化到首尾花括号截取。
6. **中文一句话解读**：除评分与理由外，额外产出 ``takeaway`` 用于邮件正文。
7. **``print`` → ``logger``**。
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import config
from .config import (
    ABSTRACT_MAX_CHARS,
    AI_API_KEY,
    AI_BASE_URL,
    AI_MAX_RETRIES,
    AI_MAX_WORKERS,
    AI_MODEL,
    AI_TEMPERATURE,
    AI_THRESHOLD,
    AI_TIMEOUT,
)

log = logging.getLogger(__name__)


def build_system_prompt(topic=None) -> str:
    """构造 system prompt。

    做成函数而不是模块级常量：这样它总是反映 **当前** 的 ``config``，
    测试或运行期改配置都不会拿到过期文本。
    ``topic`` 为空时用 ``config.RESEARCH_FIELD``（单方向模式）。
    """
    field = topic.name if topic is not None else config.RESEARCH_FIELD
    return (
        f"你是学术文献筛选助手，服务于一位研究「{field}」的研究生。"
        "你的任务是判断论文与用户研究方向的相关性，并给出中文解读。"
        "你只输出一个 JSON 对象，不输出任何解释性文字、不使用 Markdown 代码块。"
    )


#: 模块级常量，供直接引用；值由 :func:`build_system_prompt` 派生。
SYSTEM_PROMPT = build_system_prompt()

USER_TEMPLATE = """请判断以下论文与用户研究方向的**相关性**。

## 用户研究方向
{keywords}

## 论文信息
- 期刊：{journal}
- 发表日期：{pub_date}
- 标题：{title}
- 摘要：{abstract}{abstract_note}

## 输出要求
只输出如下 JSON（不要任何其它内容）：
{{
  "relevance": 0 到 100 的整数，表示与用户研究方向的相关程度,
  "takeaway": "一句话中文解读，说明这篇论文做了什么、结论是什么，60 字以内",
  "reason": "一句话中文理由，说明为什么给这个分数，60 字以内",
  "anode_free": true 或 false —— 这篇论文的电池是不是「无负极」构型,
  "cathode_system": "li-rich-mn" / "sodium" / "other" 三者之一
}}

**两个结构化判断**（与 relevance 各自独立，不要因为分数低就随手写 false）：
- `anode_free`：{anode_free_hint}
- `cathode_system`：{cathode_hint}
- 若 `anode_free` 为 true，`takeaway` 里**必须出现「无负极」三个字**。

评分参考（判定基准就是上面给出的那份「用户研究方向」，不要自行假设学科）：
- 90-100：论文正面解决上述方向的核心问题，读完可直接用于该方向的研究
- 70-89：属于同一材料体系或同一技术路线，只是研究角度不同（方法、表征、机理）
- 50-69：相邻领域，可能沾边但需要人工判断，或只把该方向当作应用场景之一
- 0-49：明显不属于该方向，或属于综述/展望/纯工程应用类内容

注意：
- 判断依据以**标题 + 摘要**实际写的内容为准，不要被期刊名影响。"""

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

#: 某些兼容端点不支持 response_format，这里做一次性探测降级
_json_mode_supported = True


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_ai_json(content: str | None) -> dict | None:
    """从模型输出中稳健提取 JSON 对象。

    LLM 常返回 ```json 包裹、前后带说明文字，甚至截断，这里做三级尝试。
    """
    if not content:
        return None
    text = content.strip()

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass

    stripped = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass

    match = _JSON_RE.search(stripped)
    if match:
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
    return None


def normalize_result(data: dict) -> tuple[int, str, str]:
    """把模型输出归一化为 ``(score, takeaway, reason)``，score 必定是 0-100 的 int。"""
    raw_score = data.get("relevance", data.get("score", 0))
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    takeaway = str(data.get("takeaway") or data.get("summary") or "").strip()
    reason = str(data.get("reason") or "").strip()
    return score, takeaway, reason


#: 归一化体系名时要去掉的**首尾**标点（模型偶尔会把值写成 ``"sodium".`` / ``sodium,``）
_SYSTEM_STRIP_CHARS = " \t\r\n._-。，、；;:：!！?？\"'“”‘’()（）[]【】"


def _system_key(raw: object) -> str:
    """把任意"体系名"写法归一成**查表用的键**：小写、空白/下划线→连字符、去首尾标点。

    别名表的键与查表的键**必须**由同一个函数生成 —— 否则就会重演那个真实 bug：
    手写的键 ``"lithium rich manganese"`` 被查成了 ``"lithium-rich-manganese"``，
    多词别名全部静默失效（AI 认出了体系，代码却当作"没答"）。
    """
    text = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower())
    return text.strip(_SYSTEM_STRIP_CHARS)


#: ``cathode_system`` 的**同义写法** → 标准值。模型多数时候会原样回传提示词里的
#: ``"li-rich-mn" / "sodium" / "other"``，但偶尔会自作主张换个说法，
#: 认出就拿标准值，认不出就当**没答**（= 不保底）—— **绝不猜**：
#: 猜错意味着凭空置顶一篇不相关的论文。
#:
#: ⚠️ 键**必须**经 :func:`_system_key` 归一化（见下面字典推导式）。
#:    这里踩过一次坑：手写的键用空格（``"lithium rich manganese"``），
#:    而查表前已经把空格换成了连字符（``"lithium-rich-manganese"``），
#:    于是**所有多词别名全部查不到**、静默失效 —— 表现为"AI 明明认出了体系却仍然不保底"，
#:    而单看代码完全看不出问题。现在键由同一个函数生成，从根上不可能再漂移。
_CATHODE_SYSTEM_ALIASES = {
    # —— 富锂锰基 ——
    "li-rich": "li-rich-mn",
    "lirich": "li-rich-mn",
    "li rich mn": "li-rich-mn",
    "lirichmn": "li-rich-mn",
    "lithium-rich": "li-rich-mn",
    "lithium rich": "li-rich-mn",
    "lithium rich manganese": "li-rich-mn",
    "li-excess": "li-rich-mn",
    "lithium-excess": "li-rich-mn",
    "mn-rich": "li-rich-mn",
    "manganese-rich": "li-rich-mn",
    "manganese rich": "li-rich-mn",
    "lmr": "li-rich-mn",
    "lrlo": "li-rich-mn",
    "olo": "li-rich-mn",
    "over-lithiated": "li-rich-mn",
    "over-lithiated oxide": "li-rich-mn",
    "overlithiated": "li-rich-mn",
    "overlithiated oxide": "li-rich-mn",
    "li2mno3": "li-rich-mn",
    "富锂": "li-rich-mn",
    "富锂锰": "li-rich-mn",
    "富锂锰基": "li-rich-mn",
    "富锂锰基层状氧化物": "li-rich-mn",
    "富锂锰基正极": "li-rich-mn",
    # —— 钠电 ——
    "sodium": "sodium",
    "sodium-ion": "sodium",
    "sodium ion": "sodium",
    "sodium ion battery": "sodium",
    "sodium battery": "sodium",
    "sodium metal": "sodium",
    "sodium metal battery": "sodium",
    "na-ion": "sodium",
    "na ion": "sodium",
    "na metal": "sodium",
    "na battery": "sodium",
    # ⚠️ ``"na"`` 本义也可能是"不适用"，但按用户口径（宁可多留不可漏）取"钠电"。
    "na": "sodium",
    "prussian blue": "sodium",
    "prussian blue analogue": "sodium",
    "nasicon": "sodium",
    "sodium layered oxide": "sodium",
    "layered sodium oxide": "sodium",
    "na0.67mno2": "sodium",
    "na0 67mno2": "sodium",
    "钠": "sodium",
    "钠电": "sodium",
    "钠离子": "sodium",
    "钠离子电池": "sodium",
    "钠金属电池": "sodium",
    "钠基层状氧化物": "sodium",
    # —— 其它（明确列出**不是**本课题的体系，省得以后误加进来）——
    "other": "other",
    "others": "other",
    "none": "other",
    "unknown": "other",
    "not-applicable": "other",
    "n/a": "other",
    "li-s": "other",
    "lithium-sulfur": "other",
    "lithium sulfur": "other",
    "lifepo4": "other",
    "lfp": "other",
    "ncm": "other",
    "nickel-rich": "other",
    "其它": "other",
    "其他": "other",
    "锂硫": "other",
    "三元": "other",
    "磷酸铁锂": "other",
}

#: 查表用的最终别名表（键已归一化，与 ``_normalize_cathode_system`` 同一套变换）。
_CATHODE_ALIASES: dict[str, str] = {
    _system_key(alias): target for alias, target in _CATHODE_SYSTEM_ALIASES.items()
}

#: 查表查不到时可以**从尾部剥掉**的通用词。模型偶尔把体系名写成一整段
#: （``"OLO cathode"`` / ``"sodium layered oxide"``），剥掉这些词就能落到别名上。
#: ⚠️ 只剥**尾部**且只剥这类没有判别力的词：一旦尾部是实词（如 ``li-rich`` 的 ``rich``）
#:    就立刻停手，继续剥会把 ``"li-rich"`` 削成 ``"li"`` 这种毫无意义的键。
_SYSTEM_TAIL_WORDS = frozenset(
    {
        "cathode", "cathodes", "positive", "electrode", "electrodes",
        "material", "materials", "based", "oxide", "oxides", "layered",
        "system", "systems", "battery", "batteries", "cell", "cells",
    }
)


def _lookup_alias(key: str) -> str:
    """先精确查别名表；查不到就逐步剥掉尾部通用词再查（见 ``_SYSTEM_TAIL_WORDS``）。"""
    parts = key.split("-")
    while parts:
        hit = _CATHODE_ALIASES.get("-".join(parts))
        if hit:
            return hit
        if parts[-1] not in _SYSTEM_TAIL_WORDS:
            break
        parts.pop()
    return ""


def _normalize_cathode_system(raw: object) -> str:
    """把模型给的体系名归一成标准值；认不出返回 ``""``（= 不参与保底）。"""
    text = _system_key(raw)
    if text in config.KEEP_CATHODE_SYSTEMS:
        return text
    return _lookup_alias(text)


def normalize_flags(data: dict) -> tuple[bool, str]:
    """解析 AI 的「无负极 / 正极体系」两个结构化判断，返回 ``(anode_free, cathode_system)``。

    单独一个函数（而不是塞进 :func:`normalize_result`）是有意的：``normalize_result``
    的 3 元返回值是既有契约（调用点与测试 mock 都在用），而这两个字段只有保底用得到。

    ⚠️ 容错方向是**认不出来就不保底**：漏了还能靠关键词保底兜，
    猜错了就是凭空把一篇不相关的论文置顶。
    """
    raw = data.get("anode_free")
    if isinstance(raw, str):
        anode_free = raw.strip().lower() in {"true", "yes", "y", "1", "是", "无负极"}
    else:
        anode_free = bool(raw)
    return anode_free, _normalize_cathode_system(data.get("cathode_system") or data.get("cathode"))


# ---------------------------------------------------------------------------
# 单篇调用
# ---------------------------------------------------------------------------
def build_prompt(work: dict, keywords: list[str] | None = None, topic=None) -> str:
    """拼出单篇论文的 user prompt。

    判据优先级：显式 ``keywords`` > ``topic`` 的方向描述 > ``config`` 的研究方向。
    注意 ``keywords`` 与 ``topic`` 是**两种不同**的粒度：前者只是一串词，
    后者带方向名与补充说明（更准）。
    """
    abstract = (work.get("abstract") or "").strip()
    if abstract:
        abstract_block = abstract[:ABSTRACT_MAX_CHARS]
        note = ""
    else:
        abstract_block = "（未获取到摘要）"
        note = (
            "\n  （摘要缺失：只能依据标题判断，请适度降低置信度；"
            "若仅凭标题无法确定，宁可给低分并在 reason 里说明依据不足）"
        )

    if keywords:
        brief = "、".join(keywords)
    elif topic is not None:
        brief = topic.brief()
    else:
        brief = config.research_brief()
    return USER_TEMPLATE.format(
        keywords=brief,
        journal=work.get("journal") or "未知期刊",
        pub_date=work.get("pub_date") or "未知",
        title=work.get("title") or "",
        abstract=abstract_block,
        abstract_note=note,
        # 两个结构化判断的口径（学科知识，所以写在 config 里而不是这里）
        anode_free_hint=config.AI_ANODE_FREE_HINT,
        cathode_hint=config.AI_CATHODE_HINT,
    )


def _post_chat(payload: dict) -> str:
    resp = requests.post(
        f"{AI_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=AI_TIMEOUT,
    )
    if resp.status_code in (429, 500, 502, 503, 504):
        raise RuntimeError(f"AI 服务临时错误 HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_ai(
    work: dict, keywords: list[str] | None = None, topic=None
) -> tuple[int, str, str, bool, str]:
    """调用 AI 并返回 ``(score, takeaway, reason, anode_free, cathode_system)``。

    失败时抛出异常由上层处理。``keywords=None`` 表示使用 ``topic``（或 config）的
    完整研究方向描述。

    后两项是 AI 的**结构化判断**（是不是无负极 / 正极属哪一类），只有保底逻辑用得到，
    但和分数来自同一次调用 —— 再单独发一次请求既贵又可能不一致。
    """
    global _json_mode_supported

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(topic)},
            {"role": "user", "content": build_prompt(work, keywords, topic)},
        ],
        "temperature": AI_TEMPERATURE,
    }
    if _json_mode_supported:
        # 注意：OpenAI 要求 prompt 里出现 "json" 字样，本提示词已满足
        payload["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None

    for attempt in range(1, AI_MAX_RETRIES + 1):
        try:
            content = _post_chat(payload)
            data = parse_ai_json(content)
            if data is None:
                raise ValueError(f"无法解析 AI 返回的 JSON: {(content or '')[:120]!r}")
            score, takeaway, reason = normalize_result(data)
            anode_free, cathode_system = normalize_flags(data)
            return score, takeaway, reason, anode_free, cathode_system
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 400 and _json_mode_supported:
                # 供应商不支持 json_object，关掉后立刻重试
                log.warning("AI 端点不支持 response_format=json_object，降级为纯文本模式")
                _json_mode_supported = False
                payload.pop("response_format", None)
                last_error = exc
                continue
            last_error = exc
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            last_error = exc

        if attempt < AI_MAX_RETRIES:
            backoff = 2**attempt
            log.debug("AI 重试 %s/%s（%s 秒后）：%s", attempt, AI_MAX_RETRIES, backoff, last_error)
            time.sleep(backoff)

    raise RuntimeError(f"AI 调用失败: {last_error}")


def _dispatch_call(
    work: dict, keywords: list[str] | None, topic
) -> tuple[int, str, str, bool, str]:
    """单方向模式下只传两个参数调 ``call_ai``（保持向后兼容），有主题时再传第三个。"""
    if topic is None:
        return call_ai(work, keywords)
    return call_ai(work, keywords, topic)


def evaluate_one(work: dict, keywords: list[str] | None = None, topic=None) -> dict:
    """给单篇文献打分，返回带 ``ai_score`` / ``ai_takeaway`` / ``ai_reason`` 的副本。

    ``anode_free`` / ``cathode_system`` 是 AI 的结构化判断，供保底逻辑使用
    （见 :func:`src.content_rules.ai_keep_hit`）。
    即使 AI 失败也会返回结果（``ai_error=True`` + 保守分 0），
    由上层决定是否展示，绝不静默丢稿。
    """
    result = dict(work)
    try:
        score, takeaway, reason, anode_free, cathode_system = _dispatch_call(
            work, keywords, topic
        )
        result.update(
            ai_score=score,
            ai_takeaway=takeaway,
            ai_reason=reason,
            ai_error=False,
            anode_free=anode_free,
            cathode_system=cathode_system,
        )
    except Exception as exc:
        log.error("AI 打分失败 %s: %s", work.get("doi"), exc)
        result.update(
            ai_score=0,
            ai_takeaway="",
            ai_reason=f"AI 打分失败：{exc}",
            ai_error=True,
        )
    return result


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------
def evaluate_works(
    works: list[dict],
    keywords: list[str] | None = None,
    threshold: int = AI_THRESHOLD,
    max_workers: int = AI_MAX_WORKERS,
    topic=None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """并发打分，并按阈值把结果分成三堆。

    :return: ``(入选, 调用失败, 低于阈值)``
             * 入选 —— 分数 >= threshold，按 (分数, 日期) 降序
             * 失败 —— ``ai_error=True``，分数不可信，不要回写已读标记
             * 低于阈值 —— 明确判定不相关，可安全回写已读标记以免重复打分

    之所以要把"低于阈值"单独返回：候选量有近 200 篇而邮件只发 20 篇，
    若不记录这些已判定过的文献，下周它们会被原封不动地重新打分一遍。

    **硬保底（两条通道）**：
      * 命中 ``config.KEEP_RULES``（或主题自己的 ``keep``）的论文，无论分数
        多低都进"入选"，**并且**即使 AI 调用失败也照样入"入选"（分数记 0）——
        这是"老板要盯的方向不能丢"的最后一层保险，代价最大可以接受。
      * AI 自己在摘要里判定"这篇就是无负极、而且正极是富锂锰/钠电"时同样直接入"入选"
        （免阈值、后面会被置顶）。关键词表只能做字面匹配，而"是不是无负极"经常
        要靠读懂电芯构型才能判断（「裸 Cu 集流体」「负极过量≈0」），光靠词表必定漏。
    判断逻辑在 :func:`_partition_forced`。

    :param keywords: 打分判据；``None`` 表示用 ``topic``（或 config）的研究方向描述。
    :param topic: ``config.ResearchTopic``；多主题调研时每个主题各用各的判据。
    """
    if not works:
        return [], [], []

    log.info("AI 打分开始：%s 篇，并发 %s，阈值 %s", len(works), max_workers, threshold)
    scored: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(evaluate_one, work, keywords, topic): work for work in works}
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                scored.append(future.result())
            except Exception as exc:  # 理论上 evaluate_one 不抛，兜底
                work = futures[future]
                log.error("打分线程异常 %s: %s", work.get("doi"), exc)
            if index % 10 == 0 or index == len(works):
                log.info(" AI 进度 %s/%s", index, len(works))

    passed, failed, rejected = _partition_forced(scored, threshold, topic)
    passed.sort(key=lambda w: (w.get("ai_score") or 0, w.get("pub_date") or ""), reverse=True)

    log.info(
        "AI 打分完成：%s 篇通过阈值（>=%s），%s 篇低于阈值，%s 篇调用失败%s",
        len(passed),
        threshold,
        len(rejected),
        len(failed),
        _forced_note(passed),
    )
    return passed, failed, rejected


def _forced_note(passed: list[dict]) -> str:
    """日志尾巴：本轮有几篇是被保底规则硬留下的（0 篇就不提）。"""
    forced = [w for w in passed if w.get("keep_reason")]
    if not forced:
        return ""
    return f"（其中 {len(forced)} 篇由保底规则强制保留）"


def _partition_forced(
    scored: list[dict], threshold: int, topic=None
) -> tuple[list[dict], list[dict], list[dict]]:
    """三堆的划分规则，单独拎出来是因为逻辑不直观。

    保底命中的论文**先被摘出去**，再划分剩下的。顺序反了就会出错：
    "分数 >= threshold 入选 / 否则低于阈值"这套条件对一篇 55 分的无负极论文
    会得出"低于阈值"，于是它被回写已读标记、**永远不出现在邮件里** ——
    保底就白配了。先摘出去则它无条件进"入选"。同理，AI 失败的那堆也
    只在**非保底**的论文里挑，否则保底论文会因为 AI 挂了而进"失败"堆，
    同样进不了邮件（而且"失败"堆不写已读标记，下周还会重跑一遍）。

    保底有**两条通道**，顺序是先关键词后 AI：
      1. ``content_rules.keep_hit``  —— 词表命中（在进 AI 之前就已经生效）
      2. ``content_rules.ai_keep_hit`` —— AI 判定无负极 + 体系对口
    第二条只能在这里判：AI 的结论要到打分完成后才存在。

    ⚠️ 这里用的是**函数内**导入 ``content_rules``：模块顶层导入会绕成
    ``ai_matcher → content_rules → config``，而 ``content_rules`` 本身是干净的，
    问题在于 ``ai_matcher`` 被 ``main`` 和 ``content_rules`` 间接引用，
    顶层导入迟早踩到循环导入。函数内导入只多花一次 ``sys.modules`` 查表。
    """
    from . import content_rules

    passed: list[dict] = []
    failed: list[dict] = []
    rejected: list[dict] = []
    keyword_forced = 0
    ai_forced: list[tuple[str, dict]] = []
    for work in scored:
        reason = content_rules.keep_hit(work, topic)
        if reason:
            keyword_forced += 1
        else:
            # 第二条保底通道：关键词一个都没命中，但 AI 从摘要里读出了「无负极」。
            reason = content_rules.ai_keep_hit(work)
            if reason:
                ai_forced.append((reason, work))
        if reason:
            work["keep_reason"] = reason
            if work.get("ai_error"):
                # AI 挂了也要发出去：不给分数，但给出足够判断的信息。
                work["ai_score"] = 0
                work["ai_reason"] = (
                    f"AI 打分失败（{work.get('ai_reason') or '原因不明'}）；"
                    f"本篇命中保底规则「{reason}」，仅凭标题/摘要判断"
                )
            passed.append(work)
            continue
        if work.get("ai_error"):
            failed.append(work)
        elif (work.get("ai_score") or 0) >= threshold:
            passed.append(work)
        else:
            rejected.append(work)
    if keyword_forced:
        log.info("保底规则强制保留 %s 篇（免剔除、免 AI 阈值）", keyword_forced)
    if ai_forced:
        # 与 content_rules.log_kept 一样用 INFO：这是"靠 AI 读出来的漏网之鱼"，
        # 日志里必须看得见，否则词表该补哪里永远不知道。
        log.info("AI 判定无负极且体系对口，额外强制保留 %s 篇：", len(ai_forced))
        for reason, work in ai_forced:
            log.info("  [保底·%s] %s", reason, work.get("title"))
    return passed, failed, rejected
