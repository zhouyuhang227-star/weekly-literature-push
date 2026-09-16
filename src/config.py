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
    "Small": "1613-6829",
}

# OpenAlex 的 OR 语法：用 | 拼接 ISSN。
# 实测 filter=primary_location.source.issn:1476-4687|1095-9203
#   → OQL 解析为 "ISSN is (1095-9203 or 1476-4687)"，正确。
ISSN_FILTER = "|".join(JOURNALS.values())

# ISSN → 期刊名，仅在 OpenAlex 未返回 source.display_name 时作兜底显示。
ISSN_TO_NAME: dict[str, str] = {issn: name for name, issn in JOURNALS.items()}

# ---------------------------------------------------------------------------
# ★ 期刊的 DOI 前缀（多数据源识别期刊时用）
# ---------------------------------------------------------------------------
# 为什么需要它：不同数据源对「刊名」的记录质量差别很大。实测 Semantic Scholar 把
# Advanced Materials 的论文（DOI 10.1002/adma.74958）标成刊名 "Advances in
# Materials"、publicationVenue.issn 也给成 2327-2503（另一本真实存在的期刊），
# 而 Angewandte Chemie Int. Ed. 的 publicationVenue 干脆是 null。只按刊名/ISSN
# 匹配会**静默丢掉 Advanced Materials 的全部结果**。
#
# DOI 前缀由出版社分配、多年不变，是识别期刊最可靠的办法：
#   adma = Advanced Materials / adfm = Advanced Functional Materials / anie = Angew ...
# 识别顺序是 **DOI 前缀优先、刊名兜底**（见 src/sources/base.py）。
# 用 str.startswith 前缀匹配，不是正则，所以模式里的 "." 就是普通点号。
JOURNAL_DOI_PATTERNS: dict[str, tuple[str, ...]] = {
    # —— 正刊 ——
    "Nature": ("10.1038/nature", "10.1038/s41586-"),
    "Science": ("10.1126/science.",),
    # —— 子刊 / 专业顶刊 ——
    "Nature Energy": ("10.1038/s41560-",),
    "Nature Materials": ("10.1038/s41563-",),
    "Nature Chemistry": ("10.1038/s41557-",),
    "Nature Sustainability": ("10.1038/s41893-",),
    "Nature Communications": ("10.1038/s41467-",),
    "Science Advances": ("10.1126/sciadv.",),
    "Joule": ("10.1016/j.joule.",),
    "Journal of the American Chemical Society": ("10.1021/jacs.",),
    "Angewandte Chemie Int. Ed.": ("10.1002/anie.",),
    "Advanced Materials": ("10.1002/adma.",),
    # RSC 的 DOI 形如 10.1039/D6EE01234A，没有能区分 EES 与同社其它刊的固定前缀，
    # 所以留空 —— 该刊只能靠刊名兜底识别（OpenAlex / Crossref 各有自己的路子）。
    "Energy & Environmental Science": (),
    "Advanced Functional Materials": ("10.1002/adfm.",),
    "Small": ("10.1002/smll.",),
}

#: Semantic Scholar 的刊名写法 → 本项目 ``JOURNALS`` 里的刊名。
#: 两边都会先归一化（小写、只留字母数字）再比较，所以大小写随便写。
#: 默认刊名能精确对上的不需要列在这里。
S2_VENUE_ALIASES: dict[str, str] = {
    "Angewandte Chemie": "Angewandte Chemie Int. Ed.",
    "Angewandte Chemie International Edition": "Angewandte Chemie Int. Ed.",
    "Angewandte Chemie (International Ed. in English)": "Angewandte Chemie Int. Ed.",
    "Energy and Environmental Science": "Energy & Environmental Science",
    "Journal of the American Chemical Society (JACS)": "Journal of the American Chemical Society",
}

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
# 不在这张表里的期刊（如 Energy & Environmental Science、AFM、Small）加成为 0 ——
# 它们照样会被检索、会进邮件，只是排序时不额外加分。
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
# ★ 内容规则：按论文**内容**加分 / 剔除（与期刊档次加成并列）
# ===========================================================================
# 完整的排序公式：
#
#     最终分 = AI 相关性分 + 期刊档次加成 + 内容规则加成
#
# ⚠️ 两种加成**都只管排序，不管入选**。能不能进邮件仍然只看 AI 分是否过 AI_THRESHOLD。
#    好处：不会因为"顶刊"或"蹭到热词"就把一篇不相关的论文塞进邮件。
#
# 规则是一个字典，字段说明（写规则的地方就是这里和每个主题自己的 bonuses）：
#   label   必填。显示名，会出现在邮件的加分明细里（如「固态电池 +1」）。
#   score   必填。加几分。排除规则写 0（反正它只看"有没有命中"）。
#   any     必填。命中其中**任意一个**短语就算命中。
#   all     选填。必须**全部**命中才算命中（用来给规则加前提，例如"必须同时是钠电"）。
#   unless  选填。只要命中其中任意一个就**不算命中**（用来挡掉误伤）。
#   scope   选填。"all"（默认，标题+摘要）或 "title"（只看标题）。
#           排除规则**默认只看标题**，见下面 EXCLUDE_RULES 的说明。
#
# 上面这套字段同时用于三类规则（都在下面）：
#   BONUS_RULES  加分     —— 只管**排序**，分数再高也换不来入选。
#   EXCLUDE_RULES 剔除    —— 命中就**直接丢掉**，连 AI 都不打分。
#   KEEP_RULES   硬保底  —— 命中就**强制进邮件**，AI 分再低也留，且**免于上面所有剔除**。
#
# 匹配方式：先把文本归一化（转小写、标点与连字符换成空格、压缩空格），
#   再按「词首对齐」匹配短语。所以
#     "Solid-state" / "solid state" / "solid—state" 都能被 "solid state" 命中，
#     "solid state batter" 也能命中 "solid-state batteries"。
#   ⚠️ 短语别写太短（< 4 个字符），否则容易误伤（例如 "na" 会命中 "nanowire"）。

#: 「无负极」构型的写法集合（**加分规则与硬保底规则共用这一份**）。
#:
#: 单独抽出来是因为它要出现在两个地方：``BONUS_RULES``（加分）与
#: ``KEEP_RULES``（硬保底，见下面）。两处如果各写一份词表，早晚会漂移 ——
#: 加了新写法只改了一处，于是"能加分但保不住"，或者反过来，都是很难查的坑。
#:
#: 匹配前会先归一化（转小写、连字符换成空格），所以 ``Anode-free`` / ``anode free``
#: 都能被 ``"anode free"`` 命中；又因为是**词首对齐**匹配，``"anode free"``
#: 已经能覆盖 ``"anode-free sodium metal battery"`` 这类写法，不必再单列。
ANODE_FREE_TERMS: list[str] = [
    "anode free",
    "anodeless",
    "anode less",
    "free anode",  # 覆盖 "Li-free anode" / "Na-free anode" / "metal-free anode"
    "zero excess",
    "hostless",
    "lithium free anode",
    "sodium free anode",
    "aflmb",  # anode-free lithium metal battery（文献里常见的缩写）
]

#: 所有主题共用的内容加分。
BONUS_RULES: list[dict] = [
    {
        # 电池体系只要是固态路线就 +1
        "label": "固态电池",
        "score": 1,
        "any": [
            "solid state batter",
            "solid state lithium",
            "all solid state",
            "solid state electrolyte",
            "solid electrolyte",
            "inorganic solid electrolyte",
            "sulfide solid electrolyte",
            "garnet electrolyte",
            "llzo",
            "argyrodite",
            "nasicon",
        ],
    },
    {
        # 在"固态"基础上再 +1（累计 +2）→ 用 all 保证真的同时是固态 + 聚合物
        "label": "固态聚合物电解质",
        "score": 1,
        "any": [
            "solid polymer electrolyte",
            "polymer solid electrolyte",
            "solid state polymer electrolyte",
            "polymer electrolyte",
            "poly ethylene oxide",
            "peo based",
        ],
        "all": ["solid"],
        "unless": ["gel polymer", "gel electrolyte", "gelatin"],
    },
    {
        # 无负极构型（含锂/钠，不限体系）
        #
        # 权重定得比其它加分高一个数量级是有意的：老板要求盯这个方向，
        # 而期刊加成最高也就 +9（大子刊），AI 分满打满算 100 —— +10 足以把
        # 一篇「AI 只给了 55 分」的无负极论文顶到能和顶刊论文并排的位置。
        "label": "无负极",
        "score": 10,
        "any": ANODE_FREE_TERMS,
    },
]

#: 命中即**硬保底**（无论 AI 打多少分都进邮件，且**无视所有剔除规则**）。
#:
#: 和 ``BONUS_RULES`` 的区别（很容易混，务必分清）：
#:   * ``BONUS_RULES`` 只管**排序**，不管入选 —— 加再多分，AI 分没过
#:     ``AI_THRESHOLD`` 照样进不了邮件。
#:   * ``KEEP_RULES`` 管**入选**：命中就强制进邮件，AI 分再低也留。
#:
#: 为什么需要它：无负极构型的论文经常是"电解液工程"（如《Anode-free sodium
#: metal batteries enabled by electrolyte engineering》），而「电解液工程」
#: 是 ``EXCLUDE_RULES`` 里的硬剔除项 —— 在**还没进 AI 打分**时就被丢掉了，
#: 加分规则根本来不及生效。老板要盯的方向不能这样丢，所以单开这一层。
#:
#: ⚠️ 代价：命中即强推，所以 ``scope`` 别乱放。默认 ``all``（标题+摘要），
#:    因为"只在摘要里提到无负极"的论文同样算这个方向；想收紧成"标题必须写明"
#:    就显式写 ``"scope": "title"``。
#: 字段与 ``BONUS_RULES`` 相同，只是 ``score`` 无意义（可省略）。
KEEP_RULES: list[dict] = [
    {
        # label 只写方向名：邮件卡片/日志会自己在前面加「硬保底 · 」，
        # 写进来会变成「硬保底 · 无负极（硬保底）」。
        "label": "无负极",
        "score": 0,
        "any": ANODE_FREE_TERMS,
    },
]

#: 命中即**直接剔除**（不进 AI 打分、不进邮件）的规则。
#:
#: ⚠️ 排除规则默认**只看标题**（``scope`` 的默认值对排除规则是 ``"title"``）。
#:    原因：摘要是 AI 生成的浓缩文本，"electrolyte additive" 这类词在**相关论文**里
#:    也经常作为对比组出现，按摘要剔除会误杀。而这样的"漏网之鱼"本来就会被 AI 打低分，
#:    所以宁可漏掉也不误杀。确实想看摘要就显式写 ``"scope": "all"``。
EXCLUDE_RULES: list[dict] = [
    {
        "label": "电解液工程",
        "score": 0,
        "any": [
            "electrolyte additive",
            "electrolyte additives",
            "electrolyte formulation",
            "electrolyte engineering",
            "electrolyte optimization",
            "electrolyte design",
            "solvation structure",
            "solvent molecule",
            "high concentration electrolyte",
            "localized high concentration",
            "electrolyte solvent",
        ],
        # 固态体系不误伤：命中这些词就放行
        "unless": [
            "solid state",
            "all solid state",
            "solid electrolyte",
            "inorganic electrolyte",
            "polymer electrolyte",
        ],
    },
    {
        "label": "隔膜改性",
        "score": 0,
        "any": [
            "separator modification",
            "modified separator",
            "separator coating",
            "coated separator",
            "functional separator",
            "separator design",
            "separator engineering",
            "ceramic coated separator",
            "separator for",
        ],
    },
]

#: 全局排除说明。会拼进**每个主题**给 AI 看的描述里，让 AI 也避开这些方向。
#: （与上面的 EXCLUDE_RULES 互补：那一条管"硬剔除"，这一句管"AI 打分时别给高分"。）
#:
#: ⚠️ 末尾那句例外是必需的：无负极构型的论文常常正是"电解液工程"，
#:    如果不告诉 AI，AI 会照着"不看电解液工程"给低分、并写一段负面的理由 ——
#:    那样硬保底虽然还能把它捞进邮件，但卡片上的解读和理由是反的。
GLOBAL_EXCLUDE_NOTE = (
    "不看电解液工程（液态电解液添加剂 / 溶剂化结构 / 配方优化）与隔膜改性；"
    "不计入凝胶电解质。"
    "**例外：只要涉及无负极构型（anode-free / anodeless / hostless / zero-excess，"
    "含锂与钠体系），一律保留并给高分，即使它属于电解液工程** —— 这是重点关注方向。"
)


def validate_rule_list(rules: object, group_name: str) -> list[str]:
    """检查一组内容规则有没有写错（写错只会静默失效，比报错更难发现）。"""
    problems: list[str] = []
    if not isinstance(rules, list):
        problems.append(f"{group_name} 必须是列表，现在是 {type(rules).__name__}")
        return problems
    for index, rule in enumerate(rules, start=1):
        where = f"{group_name} 第 {index} 条"
        if not isinstance(rule, dict):
            problems.append(f"{where}必须是字典，现在是 {type(rule).__name__}")
            continue
        label = str(rule.get("label") or "").strip()
        if not label:
            problems.append(f'{where}缺少 "label"（显示名）')
            # 没有显示名时就用"哪一组的第几条"当名字，报错仍然能定位
            label = where
        else:
            # 带上组名：同时校验 BONUS_RULES / EXCLUDE_RULES / KEEP_RULES，
            # 只说「固态电池」的话不知道是错在哪一组里
            label = f"{where}「{label}」"
        patterns = [str(p).strip() for p in (rule.get("any") or []) if str(p).strip()]
        if not patterns:
            problems.append(f'{label} 缺少非空的 "any"（命中列表）→ 这条规则永远不会生效')
        for key in ("all", "unless"):
            value = rule.get(key)
            if value is not None and not isinstance(value, (list, tuple)):
                problems.append(f'{label} 的 "{key}" 必须是列表')
        try:
            int(rule.get("score") or 0)
        except (TypeError, ValueError):
            problems.append(f'{label} 的 "score" 不是整数')
    return problems


def validate_content_rules() -> list[str]:
    """检查全局的 ``BONUS_RULES`` / ``EXCLUDE_RULES`` / ``KEEP_RULES``。

    ``KEEP_RULES`` 尤其要查：它的失效方式最隐蔽 —— 规则写得不对只会"从不命中"，
    于是老板要盯的那个方向静默地继续漏，日志里看不出任何异样。
    """
    return (
        validate_rule_list(BONUS_RULES, "BONUS_RULES")
        + validate_rule_list(EXCLUDE_RULES, "EXCLUDE_RULES")
        + validate_rule_list(KEEP_RULES, "KEEP_RULES")
    )


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

# 3) 判断相关性的关键词 —— 属于【旋钮 B】。主要作为 AI 的打分标准。
#    多词短语会自动加引号，AI 侧不要求逐字命中，可以写得又细又长。
#
#    它**是否参与召回**取决于 RETRIEVAL_MODE：
#      * "keyword" / "both"：会（单方向模式没有单独的 search_terms，
#        于是这里的词**兼任召回词**）→ 改动会同时改变候选量与 AI 尺子；
#      * "topic"：不会，只当打分尺子。
#    多主题模式下每个主题有独立的 search_terms 当召回词，本项不参与召回。
#
#    ⚠️ 想加"排除项"（例如"不要聚合物电解质"）请写进 RESEARCH_DESCRIPTION，
#       因为关键词是 OR 并联的，写在这里只会让召回更多。
#    ⚠️ 关键词为空且 keyword 模式下没有任何 search_terms → 程序**直接报错**
#       （不再退化成"全库检索"），以免静默推一堆无关文献。
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
# "keyword" —— 用 title_and_abstract.search 做**字面词组召回**（当前采用）
# "topic"   —— 用 OpenAlex 的**语义主题分类**（topics.id）召回
# "both"    —— 两者取并集
#
# ★ 当前为什么是 "keyword"：
#   OpenAlex 的主题分类只有约 4500 个、粒度很粗，实测
#     /topics?search="lithium-rich cathode"       → 0 条
#     /topics?search="lithium-rich"               → 0 条
#     /topics?search="sodium-ion battery"         → 0 条
#   也就是说 topic 模式**根本表达不了**「富锂锰正极」「钠离子正极」，
#   只能退回「电池材料」这种一锅端泛主题（14 本刊 30 天 237 篇，全靠 AI 去捞）。
#   字面检索反而准得多：一组 search_terms OR 起来能把两个方向基本捞全（18 / 45 篇每 30 天）。
#   于是分工是：第 1 层（字面检索）保证**不漏**，第 2 层（AI + description）保证**不滥**。
#
# ⚠️ 想切回语义主题：改成 "topic"，并给每个主题配上 "能解析到"的 topic_query
#    （先用 `python -m src.main --find-topic "短语"` 试，日志里能解析出 Txxxxx 才算数）。
#    解析不到时**会直接报错中断该主题**，不会再静默退化成"全库检索"。
#
# 实测（11 本顶刊 / 近 30 天，单方向「固态电池」的旧数据）：
#   无过滤                        → 2069 篇
#   keyword 模式（5 个长短语）     →   36 篇
#   topic   模式（T10281）        →  194 篇
RETRIEVAL_MODE = "keyword"

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
#   name         必填。主题显示名，用于日志、预览文件名、状态文件的分区键。
#   search_terms 第 1 层召回词（keyword 模式下**真正决定候选量**的就是它）。
#                多条之间 OR 并联，必须是「能在真实标题/摘要里逐字出现」的短词。
#                留空则退化成用 keywords 召回；两者都空 → keyword 模式下**直接报错**。
#   keywords     必填。第 2 层 AI 的打分尺（= USER_KEYWORDS），不参与召回。
#   description  选填。补充"要什么/不要什么"，AI 判不准时最有效的一招。
#   topic_query  仅 topic / both 模式需要。第 1 层语义主题短语（= TOPIC_QUERY）；
#                keyword 模式下留空即可。
#   title        选填。邮件标题前缀，默认是「<name>顶刊周报」。
#   topics       选填。手工锁定主题 id，格式同上面的 TOPICS；留空则按 topic_query 自动解析。
#   key          选填。状态文件里的分区键，默认等于 name。
#                    ⚠️ **改了 name 就会换一个新分区** → 该主题会被当成首次运行（30 天预热），
#                       旧记录留在旧分区里不再生效。想改名又不想重跑，把 key 填成旧 name。
#   bonuses      选填。**只对这个主题生效**的内容加分（写法同上面的 BONUS_RULES）。
#   exclude      选填。**只对这个主题生效**的剔除规则（写法同上面的 EXCLUDE_RULES）。
#   keep         选填。**只对这个主题生效**的硬保底规则（写法同上面的 KEEP_RULES）。
#                    全局的 BONUS_RULES / EXCLUDE_RULES / KEEP_RULES 仍然照常生效，这里是叠加。
#
# 当前启用：两个方向（想回到单方向就把 RESEARCH_TOPICS 改回 []）
#
# 【三层字段的分工 —— 改方向前先看这一段】
#   search_terms  【召回层】进 OpenAlex 的字面检索（title_and_abstract.search），
#                 多条之间是 OR。必须是「能在真实标题/摘要里逐字出现」的短词。
#                 ⚠️ 写长短语等于没写：实测 "lithium-rich layered oxide (LRLO / LMR)"
#                    这类带括号/斜杠的短语命中数为 0，12 条长短语 OR 起来只有 1 篇/30 天。
#   keywords      【打分层】给 AI 看的语义线索（"关注关键词"），可以写得又细又长，
#                 它不要求能在标题里逐字出现，作用只是让 AI 判得更准。
#   description   【打分层】这个方向「要什么 / 不要什么」的一段话，权重大于 keywords。
#   topic_query   ⚠️ 当前**不生效**（RETRIEVAL_MODE = "keyword"），留空即可 ——
#                 OpenAlex 的主题分类粒度太粗，没有「富锂锰正极」「钠离子正极」这种主题，
#                 /topics?search= 对这两种说法一律返回 0 条，写了只是个坑（详见 RETRIEVAL_MODE 注释）。
RESEARCH_TOPICS: list[dict] = [
    {
        "name": "富锂锰正极",
        # 留空 = 不走 topics.id。OpenAlex 没有这个主题（"lithium-rich" 查出来是 0 条）
        "topic_query": "",
        # 实测（14 本期刊 / 近 30 天，OR 并集 = 18 篇）：
        #   "li-rich" 8 篇、"lithium-rich" 2 篇、"oxygen redox" 7 篇、
        #   "anionic redox" 5 篇、"voltage decay" 4 篇、"voltage hysteresis" 1 篇
        # ⚠️ 带上氧/阴离子氧化还原这类机理词会顺带捞到少量钠电论文 ——
        #    这是**故意的**：宁可多召回几篇交给 AI 判，也不要漏。
        #
        # 下面补的是富锂锰论文里的**高频机理词**（voltage fade / lattice oxygen /
        # oxygen release / cation migration）：很多这类论文标题里并不写 "Li-rich"，
        # 只写 "oxygen release in layered cathodes" 之类，光靠材料名会漏掉。
        # ⚠️ 缩写一律不加：实测 "lrlo" / "lmr" / "li2mno3" 命中 0 篇；
        #    化学式同理（"li1.2mn0.54..." 在索引里是**一整个词**，短语检索匹不上）。
        "search_terms": [
            # —— 材料本体 ——
            "li-rich",
            "lithium-rich",
            "lithium rich",
            "li-excess",
            "lithium excess",
            # —— 机理关键词（高召回）——
            "oxygen redox",
            "anionic redox",
            "anion redox",
            "lattice oxygen",
            "oxygen release",
            "cation migration",
            # —— 电压问题 ——
            "voltage decay",
            "voltage fade",
            "voltage hysteresis",
        ],
        "keywords": [
            # —— 材料本体 ——
            "lithium-rich layered oxide (LRLO / LMR)",
            "Li-rich Mn-based cathode",
            "Li1.2Mn0.54Ni0.13Co0.13O2 / Li2MnO3-LiMO2 composite",
            "Mn-based layered oxide",
            # —— 核心机理问题 ——
            "anionic redox / oxygen redox",
            "voltage decay / voltage hysteresis",
            "cation disorder / Li-Ni mixing",
            "lattice oxygen release / oxygen stability",
            "surface reconstruction / layered-to-spinel phase transition",
            # —— 改性手段 ——
            "surface coating / doping of Li-rich cathode",
            "electrode-electrolyte interphase on Li-rich cathode",
            # —— 体系加成相关（AI 只当加分线索，真正的硬加分见 BONUS_RULES）——
            "solid-state battery with Li-rich cathode",
        ],
        "description": (
            "只看富锂锰基层状氧化物正极（Li-rich / LRLO / LMR，含 Li2MnO3 组分）。"
            "关注电压衰减、阴离子氧化还原、氧释放、表面重构等机理问题。"
            "不含磷酸铁锂 / 三元 NCM / 富镍等其它正极体系。"
        ),
    },
    {
        "name": "钠离子正极",
        # 留空 = 不走 topics.id。OpenAlex 没有这个主题
        # （"sodium-ion battery" / "sodium-ion batteries" 查出来都是 0 条）
        "topic_query": "",
        # 实测（14 本期刊 / 近 30 天，OR 并集 ≈ 45 篇）：
        #   "sodium-ion" 34 篇、"na-ion" 8 篇、"prussian blue" 5 篇，
        #   另加几条高精度词兜住层状氧化物 / 普鲁士蓝的其它写法。
        # ⚠️ 钠电方向必然会捞到硬碳负极、电解液、隔膜类论文 ——
        #    交给第 2 层处理：AI 打分（description 里已写明「只看正极」）
        #    + EXCLUDE_RULES（电解液工程 / 隔膜改性）。
        "search_terms": [
            "sodium-ion",
            "sodium ion",
            "na-ion",
            "sodium layered oxide",
            "prussian blue",
            "na0.67mno2",
            "sodium cathode",
        ],
        "keywords": [
            # —— 材料体系（层状是重点，另有普鲁士蓝 / 聚阴离子）——
            "sodium-ion battery cathode",
            "layered sodium transition metal oxide (NaTMO2)",
            "P2-type / O3-type layered oxide",
            "Na0.67MnO2 / NaNi1/3Fe1/3Mn1/3O2 / NaNi0.5Mn0.5O2",
            "Mn-based / Fe-Mn layered sodium cathode",
            "Prussian blue analogue cathode",
            "polyanion / NASICON cathode (Na3V2(PO4)3)",
            # —— 关键问题 ——
            "phase transition (P2-O2 / O3-P3) and cycling stability",
            "air / moisture stability of sodium cathode",
            "anionic redox in sodium layered oxide",
            "Na-ion storage mechanism / Na+ diffusion kinetics",
            # —— 改性手段 ——
            "doping / surface coating of sodium cathode",
        ],
        "description": (
            "只看钠离子电池**正极**材料：层状过渡金属氧化物（P2/O3 型）、"
            "普鲁士蓝类似物、聚阴离子化合物。关注相变与循环稳定性、空气/水分稳定性、"
            "阴离子氧化还原、Na+ 扩散动力学。不含硬碳等负极、不含电解液与隔膜工作。"
            "**例外：涉及无负极构型（anode-free）的钠电论文一律保留并给高分，"
            "即使它做的是电解液工程**。"
        ),
        # 层状钠离子正极是本主题的重点方向，额外加分
        "bonuses": [
            {
                "label": "层状钠离子正极",
                "score": 2,
                "any": [
                    "layered oxide cathode",
                    "layered cathode",
                    "layered transition metal oxide",
                    "layered sodium",
                    "layered na",
                    "p2 type",
                    "o3 type",
                    "p3 type",
                    "p2 o2",
                    "o3 p3",
                ],
                "all": ["sodium"],
            },
        ],
    },
]


@dataclass
class ResearchTopic:
    """一个研究方向。单方向模式下由 ``RESEARCH_FIELD`` 等四项派生。"""

    name: str
    topic_query: str = ""
    #: 【召回层】进 OpenAlex 字面检索的短词（OR 联结）。留空则退回用 keywords 召回。
    search_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    description: str = ""
    topics: dict[str, str] = field(default_factory=dict)
    title: str = ""
    #: 去重状态文件里的分区键。默认等于 name，改了 name 又想沿用旧记录时手工指定。
    key: str = ""
    #: 只对本主题生效的内容加分 / 剔除规则（全局的 BONUS_RULES / EXCLUDE_RULES 照常叠加）。
    bonuses: list[dict] = field(default_factory=list)
    exclude: list[dict] = field(default_factory=list)
    #: 只对本主题生效的硬保底规则（全局的 KEEP_RULES 照常叠加）。
    keep: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        self.topic_query = (self.topic_query or "").strip()
        self.description = self.description or ""
        self.key = (self.key or "").strip() or self.name
        self.search_terms = [str(term).strip() for term in self.search_terms if str(term).strip()]
        self.keywords = [str(word).strip() for word in self.keywords if str(word).strip()]

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
        # 全局排除说明（在文件顶部配置）——让 AI 打分时也避开这些方向
        note = (GLOBAL_EXCLUDE_NOTE or "").strip()
        if note:
            lines.append(note)
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
            search_terms=[str(term) for term in (item.get("search_terms") or [])],
            keywords=[str(kw) for kw in (item.get("keywords") or [])],
            description=str(item.get("description") or ""),
            topics=dict(item.get("topics") or {}),
            title=str(item.get("title") or ""),
            key=str(item.get("key") or ""),
            bonuses=[rule for rule in (item.get("bonuses") or []) if isinstance(rule, dict)],
            exclude=[rule for rule in (item.get("exclude") or []) if isinstance(rule, dict)],
            keep=[rule for rule in (item.get("keep") or []) if isinstance(rule, dict)],
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
#
# ⚠️ 首次预热窗口要留够长：OpenAlex 对新论文的收录有滞后（在线发表 → 进库可能差
#    几周），而 from_publication_date 卡的是 publication_date。窗口开太窄，
#    刚上线那阵子会「看起来一篇都没有」。首次跑完就会把 DOI 记进 pushed_dois.json，
#    之后每轮只按 LOOKBACK_DAYS 滚动，不会重复推送。
LOOKBACK_DAYS = 14
LOOKBACK_DAYS_FIRST_RUN = 90

# 分页与总量上限。per-page 最大 200（OpenAlex 上限）。
PER_PAGE = 200
MAX_WORKS_FETCH = 300
MAX_PAGES = 10

# HTML 请求通用超时（秒）
HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# ★ 数据源（并集检索）
# ---------------------------------------------------------------------------
# OpenAlex 2026 年起按请求计费（匿名 1000 积分/天，耗尽后一律 429 且当天不恢复）。
# 额度用光就整轮断供 —— 而「断供」和「本周真的没有新论文」在邮件里长得一模一样。
# 所以改成**多源并集**：每轮把所有启用的源都查一遍，按 DOI 合并去重。
# 某个源失败时其余源照常工作，邮件页头会注明本轮有哪些源。
#
# 三个源的差异（都实测过，见 README「数据源」一节）：
#   openalex        —— 主力。唯一支持「标题+摘要」短语检索的源，召回质量最好。
#   crossref        —— 备用。逐刊查询（按 ISSN），期刊归属 100% 准确；
#                      无配额、无需密钥；Wiley 系刊摘要覆盖接近 100%。
#                      但它的 query.title 是分词匹配，必须本地复核（见 base.py）；
#                      且 RSC 系的 EES 在 Crossref 里查不到任何记录。
#   semantic_scholar —— 备用。一次请求即可拉回全量候选，摘要覆盖约 80-94%；
#                      但刊名字段不可靠，只能靠 DOI 前缀识别期刊（见 base.py）。
#
# 顺序 = 优先级（合并同一条论文时以先到的字段为准）。删掉某一项就等于停用该源；
# 只留 "openalex" 就是改造前的行为。用 --sources 可以在命令行临时覆盖。
DATA_SOURCES: tuple[str, ...] = ("openalex", "crossref", "semantic_scholar")

# Crossref：逐刊查询，每刊最多取回多少条（上限 1000）。
# 它的默认排序是「相关度」，所以前 N 条已包住关键词命中，不必翻页。
CROSSREF_ROWS = 100

# 网络请求的重试策略（Crossref 的两个端点共用）。
# 只对 429（限流）与 5xx（服务端抖动）重试 —— 404（DOI/ISSN 不存在）
# 重试多少次还是 404，白等只会拖慢整轮，所以要区分对待。
HTTP_RETRY_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Crossref：并发请求数。**这个值不能调高。**
#
# 实测教训：5 并发时 Crossref 会回 429，而且是**整本刊静默丢失** ——
# 同一条命令连跑两次得到「命中 39 条」和「命中 27 条」，日志里只有一行
# `Crossref 查询《Angewandte Chemie Int. Ed.》失败：HTTP 429`，
# 而那本刊（正好是 15 本里用户最想看的顶刊之一）当轮**完全没有候选**。
# 更坑的是源状态仍是 ok，邮件页头照旧写「Crossref 27 篇」，
# 用户根本看不出少了整本刊 —— 这正是这个项目最怕的静默降级。
# 3 并发 + 重试（见下）实测稳定；除非你确认 Crossref 放宽了限流，别无脑调高。
CROSSREF_CONCURRENCY = 3

# Crossref：单刊请求失败后的重试次数（含首次，所以 3 = 最多请求 3 次）。
# 只对 429 与 5xx 重试 —— 404（ISSN 写错）重试多少次都是 404，
# 白等只会拖慢整轮。
CROSSREF_RETRIES = 3

# Crossref：重试的基础等待秒数，实际等待 = 本值 × 第几次尝试（2s、4s…）。
# 它是为了等 429 的限流窗口过去，所以宁可慢一点也不能丢掉整本刊。
CROSSREF_RETRY_WAIT = 2.0

# Crossref：联系邮箱（进入礼貌池，请求更快更稳）。留空也能用，只是没有优先级。
# 想换邮箱就改这里，或在 GitHub 仓库 Secrets 里设 CROSSREF_MAILTO。
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "") or "literature-push-bot@users.noreply.github.com"

# Crossref：**不给摘要的刊**。
#
# 为什么需要这张名单：Crossref 的 ``query.title`` 是分词匹配，必须本地拿
# ``search_terms`` 复核（同时看标题和摘要）。但这几本刊在 Crossref 里**根本不带
# abstract**，复核退化成「只看标题」—— 一篇真正做富锂锰正极、标题却写成
# "Reversible anion storage in Li2MnO3-based cathodes" 的论文会被误杀。
# 所以对这几本刊：**记录没有摘要时不做复核**，直接放进候选，交给 AI 相关性
# 阈值（``AI_THRESHOLD``）兜底 —— AI 看得到完整信息，比拿一个残缺判据硬剔更可靠。
#
# 实测（2026-09-16，各刊近半年各抽 100 篇，看 Crossref 是否带 abstract）：
#   Joule                 0 / 100   ← 一条摘要都没有
#   Energy & Environmental Science  0 条记录（RSC 未按 ISSN 关联，见 README）
#   Nature Energy         8 / 100
#   Advanced Materials   91 / 100   ← Wiley 系很好，不需要放宽
#
# 只有「该刊在名单里 + 这条记录确实没摘要」两个条件同时成立才放宽，
# 所以本名单的副作用有上界，不会因为一本刊就放过整片噪音。加刊后建议实测一遍。
CROSSREF_TITLE_ONLY_JOURNALS: tuple[str, ...] = (
    "Joule",
    "Nature Energy",
    "Energy & Environmental Science",
)

# Semantic Scholar bulk search 单轮最多取回条数（接口硬上限 1000）。
# 实测富锂锰正极 14 个召回词 + 90 天 = 685 条，所以 1000 足够一页拉完。
S2_BULK_LIMIT = 1000

# Semantic Scholar 未鉴权时限流约 1 请求/秒（且不返回限流响应头），所以主动节流。
S2_MIN_INTERVAL = 1.1

#: 三个源各自的 ``requests`` 请求头（都带 UA，避免被当成爬虫）。
USER_AGENT = os.environ.get("LITERATURE_BOT_UA", "") or (
    "weekly-literature-push/1.0 (mailto:%s)" % CROSSREF_MAILTO
)

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

# Crossref 逐篇查摘要这一路的熔断阈值。
# 它比 S2 宽得多（8 vs 4），因为这里的限流往往是**自己上一波请求打出来的**：
# 逐刊检索刚发完 15 本刊×100 条的请求，紧接着 4 个线程逐篇查摘要，
# 很容易短暂吃到 429 —— 这种 429 退避一两秒就好了，熔断反而是自损。
# 这个阈值只用来封顶最坏情况耗时（每篇失败要白等 CROSSREF_RETRY_WAIT×1+×2）。
CROSSREF_CIRCUIT_BREAK_AFTER = 8

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

# OpenAlex API key（**可选**）。
#
# 2026 年起 OpenAlex 改成「额度/积分」计费：匿名请求每天 1000 积分、
# 每次查询约 10 积分（≈100 次），用完返回 HTTP 429 + "Insufficient budget"，
# 并且要等到次日 UTC 0 点才恢复 —— 重试完全没用。
# 填了 key 就走独立额度；不填则保持原样（绝大多数场景一周几次请求完全够用，
# 只有本地反复调试才会把当天额度打光）。
OPENALEX_API_KEY = (os.getenv("OPENALEX_API_KEY") or "").strip()

MAX_EMAIL_ITEMS = 20  # 常规轮：单封邮件最多展示篇数，规避 Gmail 102KB 截断并控制可读性

# 首次运行（90 天预热窗）的展示上限单独放宽。
#
# 预热窗比常规窗宽 6 倍，候选量也是同一个量级（实测 50～110 篇），而 20 篇的
# 上限会把绝大多数相关文献直接截掉 —— 而预热**只需要来一次**，多展示几十篇
# 不会造成长期负担。之后每轮自动回到 MAX_EMAIL_ITEMS。
# ⚠️ 别把这个值调得太离谱：Gmail 对超过 102KB 的邮件正文会截断（“查看全文”），
#    50 篇实测约 80KB，尚在安全线内。
MAX_EMAIL_ITEMS_FIRST_RUN = 50

# ---------------------------------------------------------------------------
# 超出正文上限的文献 → PDF 附件
# ---------------------------------------------------------------------------
# 邮件正文装不下的（第 21 篇往后 / 首次运行第 51 篇往后）不再"消失"，而是按
# **完全相同的格式**（期刊 + 日期 + 最终分 + 标题 + 作者 + 中文解读 + 判定理由
# + DOI）排进一份 PDF 附件，跟正文一起发出去。
OVERFLOW_ATTACHMENT = True

# 附件里最多排多少篇（防止首次预热把 300 篇全排进去、附件读到崩溃）。
# 超出这个数的篇数**不会被标记为已推送**，下一轮会重新评估、有机会回到正文。
ATTACH_PDF_MAX_ITEMS = 200

# PDF 里的中文字体。
#
# 用 PDF 规范自带的"非嵌入预定义 CJK 字体"路径：不嵌字体文件、附件只有几十 KB，
# 代价是只支持 Adobe-GB1（≈GBK）字符集里的字（见 src/pdf_report.py 的说明）。
# 想换字体只需改这三行（换成任何 Adobe-GB1 字体名 / 对应编码即可），
# 但**不要**改成需要嵌入的字体名，否则阅读器找不到字体会显示空白。
ATTACH_PDF_FONT = "STSong-Light"          # 简体中文标准字体
ATTACH_PDF_ENCODING = "UniGB-UCS2-H"      # UTF-16BE 两字节码
ATTACH_PDF_CID_ORDERING = "GB1"           # Adobe-GB1 字符集

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
    # ⚠️ 召回真正用的是 search_terms；只有它为空时才退回 keywords（兼容旧配置）
    terms = list(view.search_terms) or keywords

    if mode == "topic":
        recall = (
            f"topic（语义主题）—— 由 TOPIC_QUERY={query!r} 解析出的 topics.id 决定，"
            f"USER_KEYWORDS 不参与"
        )
        hint = (
            "topic 模式下 search_terms / USER_KEYWORDS 都不参与召回，它们只当 AI 的打分尺子。"
            '想让它们也参与召回 → RETRIEVAL_MODE = "both"（并集，召回最高）；'
            '想要更细的粒度 → RETRIEVAL_MODE = "keyword"（推荐，当前离线实测评测过）。'
            "想换召回主题 → 改 TOPIC_QUERY，且必须能解析到 id。详见 src/config.py 顶部说明。"
        )
    elif mode == "keyword":
        if terms:
            recall = f"keyword（字面词组）—— 由 search_terms 决定，{len(terms)} 个词 OR 并联：{terms}"
        else:
            recall = (
                "keyword（字面词组）—— ⚠️ search_terms 与 keywords 都是空的："
                "本轮会**直接报错**（不再静默搜索全部期刊）"
            )
        hint = (
            "keyword 模式的召回量完全由 search_terms 决定："
            "词写得太长（＞4 个词）或带括号/斜杠时 OpenAlex 命中数为 0，写了等于没写。"
            "想更不漏 → 加同义词；想更准 → 删词。"
            '想让 OpenAlex 主题分类也参与 → RETRIEVAL_MODE = "both"（前提是 topic_query 能解析到主题）。'
        )
    elif mode == "both":
        recall = (
            f"both（并集）—— topics.id（TOPIC_QUERY={query!r}） ∪ 字面词组（search_terms）"
        )
        hint = "both 模式召回最高，但请求数也翻倍；改 search_terms 与 TOPIC_QUERY 都会影响候选量。"
    else:
        recall = f"未知模式 {mode!r}"
        hint = "RETRIEVAL_MODE 只接受 'topic' / 'keyword' / 'both'。"

    keywords_desc = f"关注关键词 {len(keywords)} 个" if keywords else "关注关键词（空）"
    scoring = (
        f"AI 0-100 分、入选线 ≥ {AI_THRESHOLD} —— 尺子 = 「{view.name}」 + {keywords_desc}"
        + (" + 补充说明" if view.description.strip() else "")
        + "；排序 = 最终分（AI 分 + 期刊档次加成 + 内容加分）降序"
    )
    if topic is not None:
        # 多主题模式下，「改哪里」要落到这个主题自己的字典字段上，不然会去改全局常数
        if mode == "keyword":
            hint += (
                f"（多主题模式：改 RESEARCH_TOPICS 里「{view.name}」的 search_terms 换召回词、"
                "keywords / description 换打分尺）"
            )
        elif mode == "both":
            hint += (
                f"（多主题模式：改 RESEARCH_TOPICS 里「{view.name}」的 search_terms / topic_query 换召回词、"
                "keywords / description 换打分尺）"
            )
        else:
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
    problems.extend(validate_content_rules())
    return problems


def _topic_warnings(mode: str, view: ResearchTopic) -> list[str]:
    """与某个主题有关的提醒。"""
    problems: list[str] = []
    if not view.keywords:
        problems.append(
            f"USER_KEYWORDS 为空（主题「{view.name}」）："
            "AI 只能靠方向名一句话打分，判准率会明显下降"
        )
    if mode in ("keyword", "both") and not (view.search_terms or view.keywords):
        problems.append(
            f"keyword 模式但 search_terms 与 keywords 都为空（主题「{view.name}」）："
            "第 1 层没有任何召回条件，该主题本轮会**直接报错**"
            "（不再静默退化成「全部期刊近 N 天」的全库检索）"
        )
    for term in view.search_terms:
        if any(char in term for char in "()/"):
            problems.append(
                f"search_terms 里的「{term}」（主题「{view.name}」）含括号或斜杠："
                "OpenAlex 字面检索下这类词命中数几乎必然为 0（实测为 0），建议拆成不含符号的短词"
            )
        elif len(term.split()) > 4:
            problems.append(
                f"search_terms 里的「{term}」（主题「{view.name}」）有 {len(term.split())} 个词："
                "词组越长命中越少（实测 >4 个词基本搜不到），建议缩短"
            )
    if mode in ("topic", "both") and not view.topics and not view.topic_query.strip():
        problems.append(
            f"topic 模式但 TOPIC_QUERY 为空且 TOPICS 未手工指定（主题「{view.name}」）："
            "解析不到主题 id，该主题本轮会**直接报错**"
            "（不再静默退化成「全部期刊近 N 天」的全库检索）"
        )
    problems.extend(validate_rule_list(view.bonuses, f"主题「{view.name}」的 bonuses"))
    problems.extend(validate_rule_list(view.exclude, f"主题「{view.name}」的 exclude"))
    problems.extend(validate_rule_list(view.keep, f"主题「{view.name}」的 keep"))
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
