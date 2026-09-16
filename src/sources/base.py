"""多数据源的公共约定：统一结果结构、来源报告、关键词本地复核。

为什么需要这一层
----------------
项目原先只有 OpenAlex 一个数据源。但 OpenAlex 2026 年起**按请求计费**
（匿名 1000 积分/天，耗尽后一律 429 且当天不恢复），额度用完就整轮断供 ——
而「断供」和「本周真的没有新论文」在邮件里长得**一模一样**。
所以现在支持多个数据源**并集检索**（见 ``src/sources/__init__.py``）。

每个源必须遵守的约定
--------------------
1. 产出**统一结构**的 work 字典（用 ``make_work`` 构造）；
2. 自己负责**期刊过滤** —— 只返回 ``config.JOURNALS`` 里的期刊；
3. 召回语义在各源之间天然不同（OpenAlex 服务端短语匹配 / Crossref 的 Lucene
   分词 / S2 的布尔查询），所以各源可以自己决定怎么问，但**返回前必须过一遍
   ``matches_recall_terms``**。原因有实测依据：Crossref 的 ``query.title=lithium-rich``
   会把 "Lithium Metal Batteries"、"Lithium–Sulfur Batteries" 之类一起返回（它做的是
   分词匹配，不是短语匹配）。不复核就会白烧一堆 AI 调用，还会把噪音写进去重库。

⚠️ **唯一例外是 OpenAlex**：它的 ``title_and_abstract.search`` 本身就是短语级匹配
（实测 ``li-rich`` 与 ``lithium-rich`` 召回的是**不同**的结果集，说明连字符敏感），
而且它查的是标题+摘要两个字段，本地复核反而可能误杀。因此 OpenAlex 的结果
**不做**本地复核，保持与改造前完全一致的行为，零回归风险。
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据源标识
# ---------------------------------------------------------------------------
OPENALEX = "openalex"
CROSSREF = "crossref"
SEMANTIC_SCHOLAR = "semantic_scholar"

#: 全部已知数据源，**顺序即优先级**（合并同一条论文的元数据时以先到的为准）
ALL_SOURCES: tuple[str, ...] = (OPENALEX, CROSSREF, SEMANTIC_SCHOLAR)

#: 给人看的名字（日志与邮件里都用它）
SOURCE_LABELS: dict[str, str] = {
    OPENALEX: "OpenAlex",
    CROSSREF: "Crossref",
    SEMANTIC_SCHOLAR: "Semantic Scholar",
}

#: 允许的简写写法（命令行 / 配置文件里都能用）
SOURCE_ALIASES: dict[str, str] = {
    "oa": OPENALEX,
    "open_alex": OPENALEX,
    "cr": CROSSREF,
    "cross-ref": CROSSREF,
    "s2": SEMANTIC_SCHOLAR,
    "semantic-scholar": SEMANTIC_SCHOLAR,
    "semanticscholar": SEMANTIC_SCHOLAR,
    "semantic_scholar_api": SEMANTIC_SCHOLAR,
}


def canonical_source(name: str) -> str:
    """把用户写的源名归一成标准 id。

    不认识的写法**直接报错**而不是静默忽略 —— 静默忽略会让用户以为
    ``--sources crossref`` 生效了，实际还在跑默认配置。
    """
    key = (name or "").strip().lower()
    key = SOURCE_ALIASES.get(key, key)
    if key not in ALL_SOURCES:
        raise ValueError(
            f"未知数据源 {name!r}。可用：{'、'.join(ALL_SOURCES)}"
            f"（也接受简写：{'、'.join(sorted(SOURCE_ALIASES))}）"
        )
    return key


def source_label(name: str) -> str:
    """给人看的源名。接受标准 id 或简写（``s2`` → ``Semantic Scholar``）。

    这里**故意不报错**：日志与邮件里冒出一个没见过的名字时，原样吐出去也远比
    抛异常好 —— 一个显示问题不该让整轮运行失败。要严格校验用 ``canonical_source``。
    """
    key = (name or "").strip().lower()
    key = SOURCE_ALIASES.get(key, key)
    return SOURCE_LABELS.get(key) or (name or "")


# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------
_NON_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

#: 短于这个长度的词不做本地复核（避免 "na" 命中 "nanowire" 这类事故）
_MIN_NEEDLE = 4


def norm_text(text: str | None) -> str:
    """把任意文本归一成「小写、只留字母数字与汉字、分隔符压成单个空格」。

    ``Li-rich`` / ``li rich`` / ``Li rich`` → ``li rich``，
    这样各源五花八门的连字符写法才能互相比较。

    先做 HTML 反转义：Semantic Scholar 的 venue 字段会把 ``&`` 编码成 ``&amp;``
    （实测 ``ENERGY &amp; ENVIRONMENTAL MATERIALS``），不反转义就永远匹配不上
    配置里的 ``Energy & Environmental Science``。
    """
    return _NON_WORD_RE.sub(" ", html.unescape(text or "").lower()).strip()


def _padded(text: str | None) -> str:
    """两侧补空格，便于做"整词短语"包含判断（``" li rich "``）。"""
    return f" {norm_text(text)} "


def matches_recall_terms(work: dict, terms: list[str] | None) -> list[str]:
    """返回该文献**真正命中**的召回词（一个都没命中就是空列表）。

    查的是「标题 + 摘要」，与 OpenAlex 的 ``title_and_abstract.search`` 对齐。
    ``terms`` 为空时返回空列表 —— 调用方必须自己判断这意味着「没有可用的召回条件」。

    ⚠️ 太短的词（短于 ``_MIN_NEEDLE``）会被跳过，因为本地做整词短语匹配时
    ``"na"`` 这类词会命中一大片无关内容。**如果所有词都太短，本函数会返回空列表
    而把结果全部剔除** —— 所以调用方必须先拿 ``usable_terms()`` 检查一下。
    """
    haystack = _padded(f"{work.get('title') or ''} {work.get('abstract') or ''}")
    hits: list[str] = []
    for term in terms or []:
        needle = _padded(term)
        if len(needle.strip()) < _MIN_NEEDLE:
            # 太短的词本地复核不可靠（"na" 会命中 nanowire），交给 AI 判
            continue
        if needle in haystack:
            hits.append(term)
    return hits


def usable_terms(terms: list[str] | None) -> list[str]:
    """从召回词里挑出**本地复核真正能用**的（即长于 ``_MIN_NEEDLE`` 的词）。

    适配器在复核前应该用它判断一次：若返回空列表，说明这一轮所有召回词都太短，
    本地复核无从下手 —— 此时应该**放行全部结果并记 WARNING**，
    而不是用空复核把候选集清空。
    """
    return [t for t in (terms or []) if len(_padded(t).strip()) >= _MIN_NEEDLE]


# ---------------------------------------------------------------------------
# 期刊识别
# ---------------------------------------------------------------------------
def journal_by_doi(doi: str | None) -> str:
    """按 **DOI 前缀**识别期刊，识别不出返回 ``""``。

    DOI 前缀由出版社分配、多年不变，是跨数据源识别期刊最可靠的办法。
    之所以必须要有这条路径，是因为实测 Semantic Scholar 的刊名字段会出错：
    ``10.1002/adma.74958``（货真价实的 Advanced Materials 论文）被它标成
    ``venue="Advances in Materials"``、``publicationVenue.issn="2327-2503"``
    （另一本真实存在的期刊）。若只按刊名/ISSN 匹配，Advanced Materials 的
    结果会被**静默丢光**。

    模式表在 ``config.JOURNAL_DOI_PATTERNS``，用 ``str.startswith`` 前缀匹配
    （不是正则，所以模式里的 ``.`` 就是普通点号）。
    """
    from .. import config

    clean = (doi or "").strip().lower()
    if not clean:
        return ""
    for name, patterns in config.JOURNAL_DOI_PATTERNS.items():
        for pattern in patterns:
            if clean.startswith(pattern.lower()):
                return name
    return ""


def journal_by_venue(venue: str | None, aliases: dict[str, str] | None = None) -> str:
    """按 **刊名** 识别期刊（归一化后精确比较），识别不出返回 ``""``。

    ``aliases`` 用来吸收数据源的固定写法差异（键值都可以是任意大小写，
    内部会 ``norm_text`` 之后再比较）。返回的是 ``config.JOURNALS`` 里的刊名。
    """
    from .. import config

    key = norm_text(venue)
    if not key:
        return ""
    for original, target in (aliases or {}).items():
        if norm_text(original) == key:
            return target
    for name in config.JOURNALS:
        if norm_text(name) == key:
            return name
    return ""


def identify_journal(*, doi: str | None = None, venue: str | None = None,
                     aliases: dict[str, str] | None = None) -> str:
    """识别期刊：**DOI 前缀优先，刊名兜底**。识别不出返回 ``""``。

    调用方拿到 ``""`` 时必须丢弃这条记录（不能猜、也不能留空刊名 ——
    空刊名会让期刊档次加成失效，还会把不相关期刊的论文推给用户）。
    """
    return journal_by_doi(doi) or journal_by_venue(venue, aliases)


# ---------------------------------------------------------------------------
# 统一结果结构
# ---------------------------------------------------------------------------
def make_work(
    *,
    doi: str | None,
    title: str | None,
    source: str,
    journal: str = "",
    issn: str = "",
    pub_date: str = "",
    cited_by: int = 0,
    type_: str = "",
    openalex_id: str = "",
    abstract: str = "",
    abstract_from: str = "",
) -> dict | None:
    """构造统一结构的 work 字典；**无 DOI 时返回 ``None``**。

    无 DOI 的文献一律丢弃，与 OpenAlex 侧既有行为保持一致：邮件的唯一
    可点击标识就是 DOI，没有 DOI 的条目既点不开也无法稳定去重。

    字段清单（下游 ``dedup`` / ``ranking`` / ``ai_matcher`` / ``mailer`` 都依赖它）::

        doi / doi_url / uid / title / journal / issn / pub_date / cited_by /
        type / openalex_id / abstract / abstract_from / sources
    """
    from ..openalex_client import normalize_doi

    clean_doi = normalize_doi(doi)
    if not clean_doi:
        log.warning(
            "[%s] 跳过无 DOI 记录：title=%r", source_label(source), (title or "")[:80]
        )
        return None

    return {
        "doi": clean_doi,
        "doi_url": f"https://doi.org/{clean_doi}",
        #: 通用去重键。以前是 ``openalex:<id>``，多源之后不能再用源相关的 id。
        "uid": f"doi:{clean_doi}",
        "title": (title or "").strip() or "（无标题）",
        "journal": journal or "",
        "issn": (issn or "").lower(),
        "pub_date": pub_date or "",
        "cited_by": int(cited_by or 0),
        "type": type_ or "",
        "openalex_id": openalex_id or "",
        "abstract": abstract or "",
        "abstract_from": abstract_from or "",
        "sources": [source],
    }


# ---------------------------------------------------------------------------
# 单源报告 / 整轮结果
# ---------------------------------------------------------------------------
def _one_line(text: str, limit: int = 60) -> str:
    """把多行 / 超长的错误原因压成一行。

    完整原因仍会写进日志和 ``SourceReport.error``，这里只负责别把邮件页头
    撑爆 —— OpenAlex 的额度提示有 5 行，原样塞进「数据源：…」那一行会把
    **真正的正文淹掉**（实测见 README）。
    """
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass
class SourceReport:
    """一个数据源在本轮里的表现。"""

    name: str
    status: str  # "ok" / "failed" / "skipped"
    count: int = 0  # 该源返回并通过复核的篇数
    added: int = 0  # 合并后由它**独家带来**的篇数（用于判断它有没有白跑）
    error: str = ""
    #: “源成功了、但结果不完整”的告警（如 Crossref 有 1/15 本刊查询失败）。
    #: 它必须能进邮件：只报状态是看不出**少了一整本刊**的。
    #: 约定：**不要重复写源名**，describe()/notices() 会自己拼上 label。
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return source_label(self.name)

    def describe(self) -> str:
        if self.status == "failed":
            return f"{self.label} 失败（{_one_line(self.error)}）"
        if self.status == "skipped":
            # 「跳过」的原因（topic 模式不支持字面词）对用户没意义，
            # 详情在日志里，页头只要表明它没参与
            return f"{self.label} 未参与"
        if self.notes:
            # 篇数后面加一小截告警：这个数字是「缺了一部分」的，
            # 完整句子在 notices() 里（页头是一行设计，不能展开写）。
            return f"{self.label} {self.count} 篇⚠️{_one_line(self.notes[0], 48)}"
        return f"{self.label} {self.count} 篇"


@dataclass
class FetchResult:
    """多源并集检索的结果。"""

    works: list[dict] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[SourceReport]:
        return [r for r in self.reports if r.status == "failed"]

    @property
    def ok(self) -> list[SourceReport]:
        return [r for r in self.reports if r.status == "ok"]

    def summary(self) -> str:
        """一行人类可读的来源说明，会出现在日志与**邮件页头**。

        刻意把失败原因也写进邮件：源切换/降级必须让用户看得见，
        否则又变成「看起来成功的错」。
        """
        return " ｜ ".join(r.describe() for r in self.reports)

    def notices(self) -> list[str]:
        """需要在邮件里显著提示的异常说明（正常时为空）。"""
        out: list[str] = []
        if self.failed:
            names = "、".join(r.label for r in self.failed)
            out.append(
                f"⚠️ 本轮有数据源不可用（{names}），结果由其余数据源合并而来，"
                "可能比平时少。失败原因见运行日志。"
            )
        for report in self.reports:
            # 部分失败（如某几本刊查询失败）也要提示：源状态是 ok，
            # 但少了一整本刊 ——— 而页头上只有一个偏小的数字。
            out.extend(f"⚠️ {report.label} {note}" for note in report.notes)
        return out


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def merge_works(groups: list[tuple[str, list[dict]]]) -> tuple[list[dict], dict[str, int]]:
    """按 DOI 合并多个源的结果。

    ``groups`` 是 ``[(源名, 该源的 work 列表), ...]``，**顺序即优先级**。

    规则：

    * 同一 DOI 只保留一条；``sources`` 记录所有命中它的源；
    * **摘要取最长的那一条** —— 这是并集检索最实在的收益：同一篇论文常常
      OpenAlex 没有摘要而 Crossref/S2 有，取最长能直接提高 AI 打分的准确性；
    * 期刊名 / ISSN / 发表日期等缺失字段由后面的源补齐（先到的不被覆盖）。
    """
    merged: dict[str, dict] = {}
    added: dict[str, int] = {}

    for name, works in groups:
        # 即使这个源一篇新东西都没带来，也要先登记成 0 ——
        # 否则报告里看不出它本轮到底跑没跑
        added.setdefault(name, 0)
        for work in works:
            key = work.get("uid") or f"doi:{work.get('doi') or ''}"
            if key == "doi:":
                continue
            existing = merged.get(key)
            if existing is None:
                work.setdefault("sources", [name])
                if name not in work["sources"]:
                    work["sources"].append(name)
                merged[key] = work
                added[name] = added.get(name, 0) + 1
                continue

            if name not in existing.setdefault("sources", []):
                existing["sources"].append(name)

            new_abstract = (work.get("abstract") or "").strip()
            if len(new_abstract) > len((existing.get("abstract") or "").strip()):
                existing["abstract"] = new_abstract
                existing["abstract_from"] = work.get("abstract_from") or name

            for field_name in ("journal", "issn", "pub_date", "cited_by", "openalex_id"):
                if not existing.get(field_name) and work.get(field_name):
                    existing[field_name] = work[field_name]

    works = list(merged.values())
    works.sort(key=lambda w: w.get("pub_date") or "", reverse=True)
    return works, added
