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
JOURNALS: dict[str, str] = {
    "Nature": "1476-4687",
    "Science": "1095-9203",
    "Joule": "2542-4351",
    "Angewandte Chemie Int. Ed.": "1521-3773",
    "Advanced Materials": "1521-4095",
    "Energy & Environmental Science": "1754-5706",
    "Nature Energy": "2058-7546",
    "Nature Chemistry": "1755-4349",
    "Nature Sustainability": "2398-9629",
    "Nature Materials": "1476-4660",
    "Advanced Functional Materials": "1616-3028",
}

# OpenAlex 的 OR 语法：用 | 拼接 ISSN。
# 实测 filter=primary_location.source.issn:1476-4687|1095-9203
#   → OQL 解析为 "ISSN is (1095-9203 or 1476-4687)"，正确。
ISSN_FILTER = "|".join(JOURNALS.values())

# ISSN → 期刊名，仅在 OpenAlex 未返回 source.display_name 时作兜底显示。
ISSN_TO_NAME: dict[str, str] = {issn: name for name, issn in JOURNALS.items()}

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

# 1) 用一句短语说明你研究什么。会被用于：
#    * 拼进 AI 的 system prompt（告诉它你是谁）
#    * 邮件标题（自动拼成「<它>顶刊周报」，见文件末尾的 EMAIL_TITLE）
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
TOPIC_QUERY = "solid-state battery"

# 主题 id 通常**不需要手填**：留空即用 TOPIC_QUERY 自动查询，
# 结果缓存在 data/topics_cache.json（只查一次，之后离线可用）。
#
# 想手工锁定（例如自动查询的结果不满意）就在这里填：
#   TOPICS = {"Advanced Battery Materials and Technologies": "T10281"}
# 可先用 `python -m src.main --find-topic "your query"` 列出候选主题。
TOPICS: dict[str, str] = {}

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
    """把研究方向拼成一段给 AI 看的文字。

    AI 需要知道"你说的这个方向具体指什么"才能打分。RESEARCH_FIELD 是必需的
    一句话，RESEARCH_DESCRIPTION 是可选的补充说明，USER_KEYWORDS 则给出具体落点。
    """
    lines = [RESEARCH_FIELD]
    if RESEARCH_DESCRIPTION.strip():
        lines.append(RESEARCH_DESCRIPTION.strip())
    if USER_KEYWORDS:
        lines.append("关注关键词：" + "、".join(USER_KEYWORDS))
    return "\n".join(lines)


#: 标签常量：给 ``relevance_plan`` / ``main`` 共用，顺手也让测试不必拼字符串。
LAYER_RECALL = "召回层"
LAYER_SCORING = "打分层"
LAYER_HINT = "提示"


def relevance_plan(mode: str = RETRIEVAL_MODE) -> list[tuple[str, str]]:
    """把「这一轮靠什么召回、靠什么打分」翻译成人话，用于启动日志与 ``--show-config``。

    存在的唯一理由：默认 ``topic`` 模式下 ``USER_KEYWORDS`` 不参与召回，
    只改它会出现「候选量一点没变」的现象，而且**不报任何错**。
    与其让人对着日志自己猜，不如每轮把两层的分工直接打出来。

    返回 ``[(标签, 说明), ...]``，标签取值见 ``LAYER_*`` 常量。
    """
    if mode == "topic":
        recall = (
            f"topic（语义主题）—— 由 TOPIC_QUERY={TOPIC_QUERY!r} 解析出的 topics.id 决定，"
            f"USER_KEYWORDS 不参与"
        )
        hint = (
            "topic 模式下改 USER_KEYWORDS 不会改变候选量，它只当 AI 的打分尺子。"
            '想让关键词也参与召回 → RETRIEVAL_MODE = "both"；'
            "想换召回方向 → 改 TOPIC_QUERY。详见 src/config.py 顶部「两把旋钮」说明。"
        )
    elif mode == "keyword":
        recall = f"keyword（字面关键词）—— 由 USER_KEYWORDS 决定：{USER_KEYWORDS}"
        hint = (
            "keyword 模式召回偏低（实测 5 个词/30 天仅 36 篇）。"
            '除非你明确只要字面命中的论文，否则建议改用 "both"。'
        )
    elif mode == "both":
        recall = (
            f"both（并集）—— topics.id（TOPIC_QUERY={TOPIC_QUERY!r}）"
            f" ∪ 字面关键词 {USER_KEYWORDS}"
        )
        hint = "both 模式召回最高；改 TOPIC_QUERY 和 USER_KEYWORDS 都会影响候选量。"
    else:
        recall = f"未知模式 {mode!r}"
        hint = "RETRIEVAL_MODE 只接受 'topic' / 'keyword' / 'both'。"

    keywords_desc = f"关注关键词 {len(USER_KEYWORDS)} 个" if USER_KEYWORDS else "关注关键词（空）"
    scoring = (
        f"AI 0-100 分、入选线 ≥ {AI_THRESHOLD} —— 尺子 = RESEARCH_FIELD={RESEARCH_FIELD!r}"
        f" + {keywords_desc}"
        + (" + RESEARCH_DESCRIPTION" if RESEARCH_DESCRIPTION.strip() else "")
    )
    return [(LAYER_RECALL, recall), (LAYER_SCORING, scoring), (LAYER_HINT, hint)]


def config_warnings(mode: str = RETRIEVAL_MODE) -> list[str]:
    """挑出「明显配错了、但不会报错」的组合，交给调用方以 WARNING 打出。

    只放真正有把握的判断，宁可少报也不要狼来了。
    """
    problems: list[str] = []
    if mode not in ("topic", "keyword", "both"):
        problems.append(f"RETRIEVAL_MODE={mode!r} 不是合法值（只能是 topic / keyword / both）")
    if not USER_KEYWORDS:
        problems.append(
            "USER_KEYWORDS 为空：AI 只能靠 RESEARCH_FIELD 一句话打分，判准率会明显下降"
        )
    if mode in ("topic", "both") and not TOPICS and not TOPIC_QUERY.strip():
        problems.append(
            "topic 模式但 TOPIC_QUERY 为空且 TOPICS 未手工指定：解析不到主题 id，"
            "本轮会退化成无主题过滤（召回到全刊所有论文）"
        )
    return problems


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
