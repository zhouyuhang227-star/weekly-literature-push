"""全局配置：所有"可能要改"的东西都集中在这里。

设计原则
--------
1. 只有本文件包含"业务参数"。**换研究方向只改「研究方向」那一段**，
   换期刊改 JOURNALS，换 AI 供应商改环境变量。
2. 所有敏感信息（API Key、邮箱授权码）**只从环境变量读取**，本文件不含任何默认密钥。
3. 路径全部基于本文件位置推导，与当前工作目录无关
   （``python -m src.main`` 在任意目录下运行都能找到 data/）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
PUSHED_FILE = os.path.join(DATA_DIR, "pushed_dois.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")  # --dry-run 产出的 HTML

# 业务时区。日志文件名、运行日期、邮件标题统一使用它，避免 UTC/北京日期差一天。
TIMEZONE = "Asia/Shanghai"

# ---------------------------------------------------------------------------
# 顶刊 ISSN（eISSN 优先，缺 eISSN 用 pISSN）
# ---------------------------------------------------------------------------
# 增删期刊只改这个字典。ISSN 已逐条核对：Nature/Science/Joule 等实测均可正确命中。
#
# ⚠️ 只把期刊写进 JOURNAL_TIERS（下面的档次表）是不够的 ——
#    检索是按这里的 ISSN 过滤的，**不在这里的期刊永远不会出现在邮件里**。
JOURNALS: dict[str, str] = {
    # —— 正刊 ——
    "Nature": "1476-4687",
    "Science": "1095-9203",
    # —— 子刊 / 专业顶刊 ——
    "Nature Energy": "2058-7546",
    "Nature Materials": "1476-4660",
    "Nature Chemistry": "1755-4349",
    "Nature Sustainability": "2398-9629",
    "Nature Communications": "2041-1723",
    "Science Advances": "2375-2548",
    "Joule": "2542-4351",
    "Journal of the American Chemical Society": "1520-5126",
    "Angewandte Chemie Int. Ed.": "1521-3773",
    "Advanced Materials": "1521-4095",
    "Energy & Environmental Science": "1754-5706",
    "Advanced Functional Materials": "1616-3028",
}

# OpenAlex 的 OR 语法：用 | 拼接 ISSN。
# 实测 filter=primary_location.source.issn:1476-4687|1095-9203
#   → OQL 解析为 "ISSN is (1095-9203 or 1476-4687)"，正确。
ISSN_FILTER = "|".join(JOURNALS.values())

# ISSN → 期刊名，仅在 OpenAlex 未返回 source.display_name 时作兜底显示。
ISSN_TO_NAME: dict[str, str] = {issn: name for name, issn in JOURNALS.items()}

# ---------------------------------------------------------------------------
# ★ 期刊档次权重：排序时给顶刊加分
# ---------------------------------------------------------------------------
# 字典的**书写顺序就是高低顺序**（第一个最高），值 = (加成分数, 该档次的期刊名)。
# 加成只影响**排序**，不影响入选 —— 是否进邮件仍然只看 AI 相关性分是否过 AI_THRESHOLD。
#
# 排序公式：最终分 = AI 相关性分 + 期刊加成，按最终分降序。
#   例：大子刊论文 AI 给 62 分 → 最终 62 + 9 = 71；
#       AM  论文 AI 给 70 分 → 最终 70 + 2 = 72。
#
# 想调权重 → 改这里的数字，或增删档次；想加期刊 → 同时加进上面的 JOURNALS。
# 不在这张表里的期刊（如 Energy & Environmental Science、AFM）加成为 0。
JOURNAL_TIERS: dict[str, tuple[int, tuple[str, ...]]] = {
    "正刊": (12, ("Nature", "Science")),
    "大子刊": (9, ("Nature Energy", "Nature Materials", "Nature Chemistry", "Nature Sustainability")),
    "Joule": (7, ("Joule",)),
    "小子刊": (5, ("Nature Communications", "Science Advances")),
    "JACS": (4, ("Journal of the American Chemical Society",)),
    "Angew": (3, ("Angewandte Chemie Int. Ed.",)),
    "AM": (2, ("Advanced Materials",)),
}


def _build_journal_rank() -> tuple[dict[str, tuple[str, int]], dict[str, tuple[str, int]]]:
    """把 ``JOURNAL_TIERS`` 摊平成两张查询表：按期刊名、按 ISSN。"""
    by_name: dict[str, tuple[str, int]] = {}
    by_issn: dict[str, tuple[str, int]] = {}
    for tier, (bonus, names) in JOURNAL_TIERS.items():
        for name in names:
            by_name[name] = (tier, bonus)
            issn = JOURNALS.get(name)
            if issn:
                by_issn[issn] = (tier, bonus)
    return by_name, by_issn


#: 期刊名 → (档次名, 加成)
JOURNAL_RANK: dict[str, tuple[str, int]] = _build_journal_rank()[0]

#: ISSN → (档次名, 加成)。排序一律走 ISSN 而不是期刊名：
#: OpenAlex 返回的 display_name 常与配置里的写法不同
#: （例如 "Angewandte Chemie International Edition" vs "Angewandte Chemie Int. Ed."）。
JOURNAL_RANK_BY_ISSN: dict[str, tuple[str, int]] = _build_journal_rank()[1]


def journal_tiers_not_in_journals() -> list[str]:
    """找出写进档次表、却漏写进 ``JOURNALS`` 的期刊名。

    这种漏写**不会报错**：那本刊永远不会被检索到，档次加成也就形同虚设。
    """
    known = set(JOURNALS)
    missing: list[str] = []
    for _, names in JOURNAL_TIERS.values():
        for name in names:
            if name not in known:
                missing.append(name)
    return missing

# ===========================================================================
# ★★★ 研究方向：换课题只需要改这一段 ★★★
# ===========================================================================
# 先看这张图，再动手改 —— 本项目最容易踩的坑就在这里。
#
# 这段（连同下面的「第 1 层检索策略」）装的是【两把旋钮】，作用完全不同：
#
#   旋钮 A ── 决定「召回什么」：哪些论文能进候选池
#              = RETRIEVAL_MODE + TOPIC_QUERY          （见下面「第 1 层检索策略」）
#
#   旋钮 B ── 决定「留下什么」：进了池子的论文，AI 给谁高分
#              = RESEARCH_FIELD + RESEARCH_DESCRIPTION + USER_KEYWORDS
#
#   ★ 改 A → 候选量会变。
#   ★ 改 B → 候选量【一点不变】，变的只是 AI 手里那把打分尺。
#
#   ⚠️ 默认 RETRIEVAL_MODE = "topic"，此时 USER_KEYWORDS【不参与召回】。
#      所以"我改了关键词，跑一遍候选量纹丝不动"是【正常现象，不是 bug】。
#      想让关键词真正参与召回，二选一：
#        * RETRIEVAL_MODE 改成 "both"  ← 主题 ∪ 关键词，最省心（推荐）
#        * 改 TOPIC_QUERY              ← 仍走纯语义，但换成新方向的主题短语
#
#   想确认"这一轮到底靠什么召回"，跑一条命令即可（不联网、秒出）：
#        python -m src.main --show-config
#
#   一句话总结：想改「能搜到什么」→ 看 A；想改「搜到的里面留下什么」→ 看 B。
#
#   ⚠️ 例外：若下面「多主题调研」里的 RESEARCH_TOPICS 非空，则【这一整段失效】，
#      每个主题改用自己字典里的 topic_query / keywords / description / topics。
#      想改哪个方向，就去改那个主题的字典（当前就是这种状态）。

# 1) 用一句短语说明你研究什么。会被用于：
#    * 拼进 AI 的 system prompt（告诉它你是谁）
#    * 邮件标题（自动拼成「<它>顶刊周报」，见文件末尾的 EMAIL_TITLE）
#    ⚠️ 仅在「单方向」模式下生效。当前 RESEARCH_TOPICS 非空（多主题），这一项暂时不起作用。
RESEARCH_FIELD = "固态电池"

# 2) 可选：补充"我关心什么 / 不关心什么"。
#    AI 判得不准时，在这里补一句往往就准了（比如"只要无机固态电解质，
#    不算聚合物电解质"）。留空则只用下面的 USER_KEYWORDS。
RESEARCH_DESCRIPTION = ""

# 3) 判断相关性的关键词 —— 属于【旋钮 B】。作用取决于 RETRIEVAL_MODE：
#    * "topic"（默认）：**只**作为 AI 的打分标准，**不参与召回**。
#                       改这里不会让候选量变化，改的是"什么样的论文算相关"。
#    * "keyword" / "both"：既参与 OpenAlex 召回，也作为 AI 的打分标准。
#    多词短语会自动加引号，OpenAlex 会做词干化匹配（battery ↔ batteries）。
#
#    ⚠️ 想加"排除项"（例如"不要聚合物电解质"）请写进 RESEARCH_DESCRIPTION，
#       因为关键词是 OR 并联的，写在这里只会让召回更多。
#    ⚠️ 关键词为空时 AI 只剩 RESEARCH_FIELD 一句话可依靠，判准率会明显下降。
#    ⚠️ 仅在「单方向」模式下生效。当前 RESEARCH_TOPICS 非空（多主题），
#       每个主题用自己的 keywords，这里的 5 个词暂时不起作用。
USER_KEYWORDS: list[str] = [
    "solid-state battery",
    "solid-state electrolyte",
    "all-solid-state",
    "lithium metal anode",
    "lithium dendrite",
]

# ---------------------------------------------------------------------------
# 第 1 层检索策略
# ---------------------------------------------------------------------------
# "topic"  —— 用 OpenAlex 的**语义主题分类**（topics.id）召回（默认，推荐）
#              优点：不依赖字面关键词，garnet electrolyte / LLZO /
#                    sulfide electrolyte 这类论文也能被召回
#              ⚠️ 代价：此模式下 USER_KEYWORDS 完全不参与召回（只当打分尺）
# "keyword" —— 用 title_and_abstract.search 做字面关键词召回（召回低）
# "both"    —— 两者取并集（召回最高，候选量也最大）
#
# ★ 想让 USER_KEYWORDS 真正影响"能搜到什么"，必须选 "both" 或 "keyword"。
#
# 实测（11 本顶刊 / 近 30 天）：
#   无过滤                        → 2069 篇
#   keyword 模式（上面 5 个词）    →   36 篇   ← 召回严重不足
#   topic   模式（T10281）        →  194 篇   ← 采用
RETRIEVAL_MODE = "topic"

# ★ 旋钮 A 的核心：topic / both 模式下，**这一行才决定"召回什么"**。
# 一般就写你方向里最核心的那个英文词，不必和 USER_KEYWORDS 完全一致。
#
# ⚠️ 换了研究方向却只改 USER_KEYWORDS、忘了改这一行 → 会静默地继续召回旧方向的论文。
#    改完记得跑一次 `python -m src.main --dry-run -v`，
#    在日志里核对「主题自动解析：'...' → Txxxxx（主题名）」是不是你要的方向。
# ⚠️ 仅在「单方向」模式下生效。当前 RESEARCH_TOPICS 非空（多主题），
#    每个主题用自己的 topic_query，这一行暂时不起作用。
TOPIC_QUERY = "solid-state battery"

# 主题 id 通常**不需要手填**：留空即用 TOPIC_QUERY 自动查询，
# 结果缓存在 data/topics_cache.json（只查一次，之后离线可用）。
#
# 想手工锁定（例如自动查询的结果不满意）就在这里填：
#   TOPICS = {"Advanced Battery Materials and Technologies": "T10281"}
# 可先用 `python -m src.main --find-topic "your query"` 列出候选主题。
TOPICS: dict[str, str] = {}

# ===========================================================================
# ★★★ 多主题调研：同时跟几个方向，每个方向单独发一封邮件 ★★★
# ===========================================================================
# 留空 → 退化成上面的「单方向」模式，发一封邮件。
# 填了 → 列表里**每个主题各自检索、各自打分、各自发一封邮件**，
#        上面的 RESEARCH_FIELD / RESEARCH_DESCRIPTION / USER_KEYWORDS / TOPIC_QUERY / TOPICS
#        这五项就**不再生效**（程序会在日志里提醒，不会静默忽略）。
#        去重也是**按主题各记各的** → 同一篇论文可能同时出现在两封邮件里。
#
# 每个主题是一个字典，字段说明：
#   name        必填。主题显示名，用于日志、预览文件名、状态文件的分区键。
#   topic_query 必填。第 1 层召回用的语义主题短语（= 单方向模式里的 TOPIC_QUERY）。
#   keywords    必填。第 2 层 AI 的打分尺（= USER_KEYWORDS）。
#   description 选填。补充"要什么/不要什么"，AI 判不准时最有效的一招。
#   title       选填。邮件标题前缀，默认是「<name>顶刊周报」。
#   topics      选填。手工锁定主题 id，格式同上面的 TOPICS；留空则按 topic_query 自动解析。
#   key         选填。状态文件里的分区键，默认等于 name。
#                    ⚠️ **改了 name 就会换一个新分区** → 该主题会被当成首次运行（30 天预热），
#                       旧记录留在旧分区里不再生效。想改名又不想重跑，把 key 填成旧 name。
#
# 当前启用：两个方向（想回到单方向就把 RESEARCH_TOPICS 改回 []）
RESEARCH_TOPICS: list[dict] = [
    {
        "name": "富锂锰正极",
        "topic_query": "lithium-rich manganese-based cathode",
        "keywords": [
            "Li-rich Mn-based cathode",
            "lithium-rich layered oxide",
            "voltage decay",
            "anionic redox",
        ],
    },
    {
        "name": "无负极钠离子电池",
        "topic_query": "anode-free sodium metal battery",
        "keywords": [
            "anode-free",
            "sodium metal anode",
            "sodium plating",
            "sodiophilic",
        ],
        "description": "只关心无负极（anode-free）构型，不含常规硬碳负极体系",
    },
]


@dataclass
class ResearchTopic:
    """一个研究方向。单方向模式下由 ``RESEARCH_FIELD`` 等四项派生。"""

    name: str
    topic_query: str = ""
    keywords: list[str] = field(default_factory=list)
    description: str = ""
    topics: dict[str, str] = field(default_factory=dict)
    title: str = ""
    #: 去重状态文件里的分区键。默认等于 name，改了 name 又想沿用旧记录时手工指定。
    key: str = ""

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        self.topic_query = (self.topic_query or "").strip()
        self.description = self.description or ""
        self.key = (self.key or "").strip() or self.name

    @property
    def email_title(self) -> str:
        return self.title.strip() or f"{self.name}顶刊周报"

    def brief(self) -> str:
        """给 AI 看的「研究方向」描述。单方向模式下等价于 ``research_brief()``。"""
        lines = [self.name]
        if self.description.strip():
            lines.append(self.description.strip())
        if self.keywords:
            lines.append("关注关键词：" + "、".join(self.keywords))
        return "\n".join(lines)


def _legacy_topic() -> ResearchTopic:
    """由「单方向」四项常数拼出的主题。"""
    return ResearchTopic(
        name=RESEARCH_FIELD,
        topic_query=TOPIC_QUERY,
        keywords=list(USER_KEYWORDS),
        description=RESEARCH_DESCRIPTION,
        topics=dict(TOPICS),
    )


def active_research_topics() -> list[ResearchTopic]:
    """当前要跑的主题列表。

    ``RESEARCH_TOPICS`` 非空时用它（多主题）；否则退回单方向模式。
    **每次都重新读模块常量**，这样改完配置立刻生效，不会拿到 import 时的旧值。
    """
    if not RESEARCH_TOPICS:
        return [_legacy_topic()]

    topics: list[ResearchTopic] = []
    for index, item in enumerate(RESEARCH_TOPICS, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"RESEARCH_TOPICS 第 {index} 项必须是字典，现在是 {type(item).__name__}。"
                '写法：{"name": "...", "topic_query": "...", "keywords": [...]}'
            )
        candidate = ResearchTopic(
            name=str(item.get("name") or ""),
            topic_query=str(item.get("topic_query") or ""),
            keywords=[str(kw).strip() for kw in (item.get("keywords") or []) if str(kw).strip()],
            description=str(item.get("description") or ""),
            topics=dict(item.get("topics") or {}),
            title=str(item.get("title") or ""),
            key=str(item.get("key") or ""),
        )
        if not candidate.name:
            raise RuntimeError(f'RESEARCH_TOPICS 第 {index} 项缺少 "name"（主题显示名）')
        topics.append(candidate)

    names = [topic.name for topic in topics]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise RuntimeError(
            "RESEARCH_TOPICS 里有重名主题："
            + "、".join(sorted(duplicates))
            + "。主题名是状态文件的分区键，不能重复（可改用 key 字段区分）。"
        )
    return topics

# ---------------------------------------------------------------------------
# OpenAlex 检索参数
# ---------------------------------------------------------------------------
OPENALEX_BASE = "https://api.openalex.org/works"
OPENALEX_TOPICS_API = "https://api.openalex.org/topics"

#: 自动解析主题时的缓存文件
TOPICS_CACHE_FILE = os.path.join(DATA_DIR, "topics_cache.json")

#: 自动解析时取多少个主题。1 个通常够用；取多了召回涨但噪音也涨。
#: 实测 T10281 单独 = 194 篇，再加 T12646（无机氟化物）= 202 篇，收益很小。
TOPIC_RESOLVE_LIMIT = 1


# 字面检索字段（仅 RETRIEVAL_MODE 为 keyword/both 时生效）。可选值：
#   "title_and_abstract.search"  ← 默认，精准，只匹配标题与摘要
#   "default.search"             默认字段（标题+摘要+全文），召回更高但噪音更多
#   "fulltext.search"            仅全文
#
# ⚠️ 不要用 URL 上的 search= 参数：那个等价于 fulltext 全文检索
#    （实测 OQL 为 "fulltext has (...)"），会把 News & Views、评论等
#    顺带提到关键词的内容全部捞进来。
SEARCH_FIELD = "title_and_abstract.search"

# 时间窗。首次运行（去重库为空）用宽窗口做一次预热，之后固定滚动窗口。
LOOKBACK_DAYS = 14
LOOKBACK_DAYS_FIRST_RUN = 30

# 分页与总量上限。per-page 最大 200（OpenAlex 上限）。
PER_PAGE = 200
MAX_WORKS_FETCH = 300
MAX_PAGES = 10

# HTML 请求通用超时（秒）
HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# AI 相关性匹配（任何 OpenAI 兼容端点）
# ---------------------------------------------------------------------------
# 默认指向 DeepSeek。切换供应商只需改环境变量 AI_BASE_URL / AI_MODEL，无需改代码。
AI_BASE_URL = os.getenv("AI_BASE_URL") or "https://api.deepseek.com"
AI_API_KEY = os.getenv("AI_API_KEY") or ""
AI_MODEL = os.getenv("AI_MODEL") or "deepseek-flash"

AI_TEMPERATURE = 0
AI_THRESHOLD = 60  # 相关性评分低于此值的文献不进邮件
AI_MAX_WORKERS = 6  # 并发打分线程数
AI_MAX_RETRIES = 3  # 单篇重试次数（指数退避）
AI_TIMEOUT = 60  # 单次请求超时（秒）
ABSTRACT_MAX_CHARS = 2500  # 送入 AI 的摘要截断长度

# ---------------------------------------------------------------------------
# 摘要回退
# ---------------------------------------------------------------------------
# OpenAlex 对最新顶刊论文的 abstract_inverted_index 经常为 null
# （实测 Nature Energy / Joule 的新论文普遍如此），故需要多级回退。
ABSTRACT_SOURCE_WORKERS = 4
CROSSREF_URL = "https://api.crossref.org/works/{doi}"
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
S2_MIN_INTERVAL = 1.1  # Semantic Scholar 无鉴权约 1 req/s，串行节流
S2_MAX_RETRIES = 3  # 公共池经常 429，需退避重试
S2_CIRCUIT_BREAK_AFTER = 4  # 连续 N 篇都因 429 彻底失败就熔断，本轮不再请求 S2

# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST") or ""
SMTP_PORT = int(os.getenv("SMTP_PORT") or "465")  # 注意 or："465" 防止空字符串崩溃
SMTP_USER = os.getenv("SMTP_USER") or ""
SMTP_PASS = os.getenv("SMTP_PASS") or ""  # 授权码 / App Password，不是登录密码
MAIL_TO = os.getenv("MAIL_TO") or ""

# OpenAlex 礼貌池（faster pool）。留空则不带 mailto，仍可用但速率更低。
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO") or SMTP_USER or ""

MAX_EMAIL_ITEMS = 20  # 单封邮件最多展示篇数，规避 Gmail 102KB 截断并控制可读性

# ---------------------------------------------------------------------------
# 由「研究方向」派生的展示文案
# ---------------------------------------------------------------------------
#: 邮件标题前缀。改 RESEARCH_FIELD 会自动跟着变，无需单独维护。
EMAIL_TITLE = f"{RESEARCH_FIELD}顶刊周报"


def research_brief() -> str:
    """把「单方向」四项常数拼成一段给 AI 看的文字。

    AI 需要知道"你说的这个方向具体指什么"才能打分。RESEARCH_FIELD 是必需的
    一句话，RESEARCH_DESCRIPTION 是可选的补充说明，USER_KEYWORDS 则给出具体落点。
    """
    return _legacy_topic().brief()


#: 标签常量：给 ``relevance_plan`` / ``main`` 共用，顺手也让测试不必拼字符串。
LAYER_RECALL = "召回层"
LAYER_SCORING = "打分层"
LAYER_HINT = "提示"


def relevance_plan(mode: str = RETRIEVAL_MODE, topic: ResearchTopic | None = None) -> list[tuple[str, str]]:
    """把「这一轮靠什么召回、靠什么打分」翻译成人话，用于启动日志与 ``--show-config``。

    存在的唯一理由：默认 ``topic`` 模式下 ``USER_KEYWORDS`` 不参与召回，
    只改它会出现「候选量一点没变」的现象，而且**不报任何错**。
    与其让人对着日志自己猜，不如每轮把两层的分工直接打出来。

    ``topic`` 为空时用「单方向」四项常数（保持向后兼容）。
    返回 ``[(标签, 说明), ...]``，标签取值见 ``LAYER_*`` 常量。
    """
    view = topic if topic is not None else _legacy_topic()
    query = view.topic_query
    keywords = list(view.keywords)

    if mode == "topic":
        recall = (
            f"topic（语义主题）—— 由 TOPIC_QUERY={query!r} 解析出的 topics.id 决定，"
            f"USER_KEYWORDS 不参与"
        )
        hint = (
            "topic 模式下改 USER_KEYWORDS 不会改变候选量，它只当 AI 的打分尺子。"
            '想让关键词也参与召回 → RETRIEVAL_MODE = "both"；'
            "想换召回方向 → 改 TOPIC_QUERY。详见 src/config.py 顶部「两把旋钮」说明。"
        )
    elif mode == "keyword":
        recall = f"keyword（字面关键词）—— 由 USER_KEYWORDS 决定：{keywords}"
        hint = (
            "keyword 模式召回偏低（实测 5 个词/30 天仅 36 篇）。"
            '除非你明确只要字面命中的论文，否则建议改用 "both"。'
        )
    elif mode == "both":
        recall = f"both（并集）—— topics.id（TOPIC_QUERY={query!r}） ∪ 字面关键词 {keywords}"
        hint = "both 模式召回最高；改 TOPIC_QUERY 和 USER_KEYWORDS 都会影响候选量。"
    else:
        recall = f"未知模式 {mode!r}"
        hint = "RETRIEVAL_MODE 只接受 'topic' / 'keyword' / 'both'。"

    keywords_desc = f"关注关键词 {len(keywords)} 个" if keywords else "关注关键词（空）"
    scoring = (
        f"AI 0-100 分、入选线 ≥ {AI_THRESHOLD} —— 尺子 = 「{view.name}」 + {keywords_desc}"
        + (" + 补充说明" if view.description.strip() else "")
        + "；排序 = 最终分（AI 分 + 期刊档次加成）降序"
    )
    if topic is not None:
        # 多主题模式下，「改哪里」要落到这个主题自己的字典字段上，不然会去改全局常数
        hint += f"（多主题模式：改 RESEARCH_TOPICS 里「{view.name}」的 topic_query / keywords）"
    return [(LAYER_RECALL, recall), (LAYER_SCORING, scoring), (LAYER_HINT, hint)]


def _global_notices() -> list[str]:
    """不是错误、但很容易让人误判的现状说明（以 INFO 打印）。"""
    if not RESEARCH_TOPICS:
        return []
    return [
        f"已配置 RESEARCH_TOPICS（{len(RESEARCH_TOPICS)} 个主题）："
        "RESEARCH_FIELD / RESEARCH_DESCRIPTION / USER_KEYWORDS / TOPIC_QUERY / TOPICS "
        "这五项不再生效，每个主题改用自己字典里的字段（这是预期行为，不是错误）"
    ]


def config_notices() -> list[str]:
    """给调用方以 INFO 级别打出的「现状说明」。"""
    return _global_notices()


def _global_warnings(mode: str) -> list[str]:
    """与具体主题无关的提醒。"""
    problems: list[str] = []
    if mode not in ("topic", "keyword", "both"):
        problems.append(f"RETRIEVAL_MODE={mode!r} 不是合法值（只能是 topic / keyword / both）")
    for name in journal_tiers_not_in_journals():
        problems.append(
            f"JOURNAL_TIERS 里的「{name}」没有写进 JOURNALS：这本刊永远不会被检索到，"
            "它的档次加成也就形同虚设"
        )
    return problems


def _topic_warnings(mode: str, view: ResearchTopic) -> list[str]:
    """与某个主题有关的提醒。"""
    problems: list[str] = []
    if not view.keywords:
        problems.append(
            f"USER_KEYWORDS 为空（主题「{view.name}」）："
            "AI 只能靠方向名一句话打分，判准率会明显下降"
        )
    if mode in ("topic", "both") and not view.topics and not view.topic_query.strip():
        problems.append(
            f"topic 模式但 TOPIC_QUERY 为空且 TOPICS 未手工指定（主题「{view.name}」）："
            "解析不到主题 id，该主题会退化成无主题过滤（召回到全刊所有论文）"
        )
    return problems


def config_warnings(mode: str = RETRIEVAL_MODE, topic: ResearchTopic | None = None) -> list[str]:
    """挑出「明显配错了、但不会报错」的组合，交给调用方以 WARNING 打出。

    只放真正有把握的判断，宁可少报也不要狼来了。
    ``topic`` 为空时按「单方向」四项常数检查；但若已经配了 ``RESEARCH_TOPICS``，
    那五项本来就不生效，再对它们报警只会误导，故跳过。
    """
    problems = _global_warnings(mode)
    if topic is not None:
        return problems + _topic_warnings(mode, topic)
    if RESEARCH_TOPICS:
        return problems
    return problems + _topic_warnings(mode, _legacy_topic())


def topic_warnings(mode: str = RETRIEVAL_MODE, topic: ResearchTopic | None = None) -> list[str]:
    """只要主题级提醒。多主题时全局提醒由调用方打一次，免得 N 个主题刷屏 N 遍。"""
    if topic is None:
        if RESEARCH_TOPICS:
            return []
        topic = _legacy_topic()
    return _topic_warnings(mode, topic)


# ---------------------------------------------------------------------------
# 环境校验
# ---------------------------------------------------------------------------

#: 必填的 Secret 名称（供报错信息使用）
AI_REQUIRED = ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL")
MAIL_REQUIRED = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")


def mail_recipients() -> list[str]:
    """MAIL_TO 支持用逗号或分号分隔多个收件人。"""
    raw = MAIL_TO.replace(";", ",")
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def validate_env(require_ai: bool = True, require_mail: bool = True) -> None:
    """快速失败：关键配置缺失时立刻抛出可读错误，而不是跑到一半才崩。

    ``--dry-run`` 模式下 require_mail=False，因此无需配置 SMTP 也能本地调试。
    """
    values = {
        "AI_BASE_URL": AI_BASE_URL,
        "AI_API_KEY": AI_API_KEY,
        "AI_MODEL": AI_MODEL,
        "SMTP_HOST": SMTP_HOST,
        "SMTP_PORT": str(SMTP_PORT),
        "SMTP_USER": SMTP_USER,
        "SMTP_PASS": SMTP_PASS,
        "MAIL_TO": MAIL_TO,
    }

    missing: list[str] = []
    if require_ai:
        missing += [name for name in AI_REQUIRED if not values[name]]
    if require_mail:
        missing += [name for name in MAIL_REQUIRED if not values[name]]

    if missing:
        raise RuntimeError(
            "缺少必需的环境变量/Secrets: "
            + ", ".join(missing)
            + "\n  本地调试：复制 .env 示例或直接使用 `python -m src.main --dry-run`（不需要 SMTP）"
            + "\n  GitHub Actions：在仓库 Settings → Secrets and variables → Actions 中配置"
        )


def ai_configured() -> bool:
    """AI 是否可用（用于 --no-ai 之外的降级判断）。"""
    return bool(AI_BASE_URL and AI_API_KEY and AI_MODEL)
