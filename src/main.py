"""主流程编排 + 命令行入口。

流程
----
校验配置 → 判定时间窗 → **第 1 层检索** → 去重 → 摘要回退
→ **第 2 层 AI 语义打分** → 阈值过滤/排序/截断 → 渲染
→ （dry-run 写文件 | 发信）→ 回写状态

两层的分工（很重要，不要混淆）
------------------------------
* **第 1 层：OpenAlex 检索**，由 ``--retrieval-mode`` 控制。默认走 ``topic``
  语义主题分类（实测约 194 篇/30 天），而不是字面关键词（仅 36 篇）。
  关键词模式会漏掉「不含关键词原文但确实相关」的论文。
* **第 2 层：AI 语义打分**，输出 0-100 分 + 中文一句话解读，
  由 ``--threshold`` 决定入选线。**AI 不负责关键词匹配**，负责排序与解读。

**换研究方向只需要改 ``src/config.py`` 的「研究方向」段落**（RESEARCH_FIELD /
RESEARCH_DESCRIPTION / USER_KEYWORDS / TOPIC_QUERY），本文件无需改动。

关键安全约定
------------
* **只有邮件发送成功才回写状态**，避免"邮件挂了但 DOI 被标记为已推送"
  导致文献永久丢失。
* **``--dry-run`` 完全无副作用**：不发邮件、不写 ``pushed_dois.json``。
* 任一环节异常都以**非零退出码**结束，让 GitHub Actions 变红以便察觉，
  而不是静默成功。

用法
----
    python -m src.main --dry-run --verbose        # 本地预览，不发邮件
    python -m src.main                            # 正式运行
    python -m src.main --lookback-days 90         # 临时扩大时间窗
    python -m src.main --retrieval-mode both      # 主题 + 关键词并集（召回最高）
    python -m src.main --to me@example.com        # 临时改收件人（调试用）
    python -m src.main --no-ai                    # 跳过 AI，仅检查检索与邮件
    python -m src.main --find-topic               # 查询语义主题 id（供 config 手填）
    python -m src.main --show-config              # 只看「靠什么召回/靠什么打分」，不联网
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback

from . import (
    abstract_source,
    ai_matcher,
    config,
    content_rules,
    dedup,
    mailer,
    openalex_client,
    ranking,
    sources,
)
from .config import (
    AI_THRESHOLD,
    ISSN_FILTER,
    LOOKBACK_DAYS,
    LOOKBACK_DAYS_FIRST_RUN,
    MAX_EMAIL_ITEMS,
    MAX_WORKS_FETCH,
    OUTBOX_DIR,
    RESEARCH_FIELD,
    RETRIEVAL_MODE,
    TOPIC_QUERY,
    mail_recipients,
    relevance_plan,
    validate_env,
)
from .logger import setup_logging, today_str

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description=(
            "文献自动推送机器人：OpenAlex 检索 → AI 相关性筛选 → 期刊加权排序"
            " → 邮件周报（支持多主题，每个主题一封邮件）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m src.main --dry-run --verbose\n"
            "  python -m src.main --lookback-days 90 --max-items 50\n"
            "  python -m src.main --topic 富锂锰正极 --dry-run\n"
            "  python -m src.main --no-ai --to me@example.com\n"
            "  python -m src.main --find-topic \"perovskite solar cell\"\n"
            "\n多个主题只需在 src/config.py 的 RESEARCH_TOPICS 里加一项，本文件无需改动。\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成邮件 HTML 到 data/outbox/，不发邮件、不修改去重状态",
    )
    parser.add_argument(
        "--force-first-run",
        action="store_true",
        help="强制按首次运行处理（使用更宽的预热时间窗）",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        metavar="N",
        help=f"覆盖时间窗天数（默认：首次 {LOOKBACK_DAYS_FIRST_RUN} 天，之后 {LOOKBACK_DAYS} 天）",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=MAX_EMAIL_ITEMS,
        metavar="N",
        help=f"单封邮件最多展示篇数（默认 {MAX_EMAIL_ITEMS}）",
    )
    parser.add_argument(
        "--max-fetch",
        type=int,
        default=MAX_WORKS_FETCH,
        metavar="N",
        help=f"最多拉取的候选文献数（默认 {MAX_WORKS_FETCH}）",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=AI_THRESHOLD,
        metavar="N",
        help=f"AI 相关性评分阈值，低于此值不展示（默认 {AI_THRESHOLD}）",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=("topic", "keyword", "both"),
        default=RETRIEVAL_MODE,
        help=(
            "第 1 层检索策略：topic=语义主题分类（默认，召回高）；"
            "keyword=字面关键词（精确但会漏）；both=并集"
        ),
    )
    parser.add_argument(
        "--keywords",
        default=None,
        metavar="KW",
        help=(
            "研究方向关键词，用分号或逗号分隔（默认用 config.USER_KEYWORDS）。"
            "topic 模式下它们不参与检索，而是作为 AI 的打分标准"
        ),
    )
    parser.add_argument(
        "--find-topic",
        nargs="?",
        const=TOPIC_QUERY,
        default=None,
        metavar="QUERY",
        help=(
            "不跑主流程，只查询 OpenAlex 语义主题并打印候选 id/名称，"
            f"供你手工填进 config.TOPICS（省略 QUERY 则用 config.TOPIC_QUERY={TOPIC_QUERY!r}）"
        ),
    )
    parser.add_argument(
        "--topic",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "只跑指定主题（按 RESEARCH_TOPICS 里的 name 或 key 匹配，可重复："
            "--topic A --topic B）；不传则跑全部主题"
        ),
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help=(
            "不跑主流程，只打印「这一轮靠什么召回、靠什么打分」然后退出（纯离线，不联网）。"
            "改完 config.py 拿不准改动生效在哪一层时先跑它"
        ),
    )
    parser.add_argument(
        "--sources",
        default=None,
        metavar="LIST",
        help=(
            "本轮启用的数据源，逗号分隔（默认用 config.DATA_SOURCES："
            "openalex,crossref,semantic_scholar 全开）。"
            "例：--sources openalex 只跑主源；--sources crossref,s2 只用备用源"
        ),
    )
    parser.add_argument(
        "--to",
        default=None,
        metavar="ADDR",
        help="覆盖收件人（多个用逗号分隔），调试时很有用",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="跳过 AI 打分（所有文献按 0 分处理并全部展示），仅用于排查检索与邮件链路",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出 DEBUG 级日志（含 OpenAlex 的 OQL 检索语义）",
    )
    return parser


def parse_keywords(raw: str | None) -> list[str] | None:
    """解析 ``--keywords`` 覆盖值。

    不传时返回 ``None``（而不是 USER_KEYWORDS），这样 AI 模块会用
    ``config.research_brief()`` 拼出「方向 + 补充说明 + 关键词」的完整描述，
    而不是只拿到关键词丢失上下文。
    """
    if not raw:
        return None
    parts = raw.replace("；", ";").replace("，", ",").replace(";", ",").split(",")
    keywords = [part.strip() for part in parts if part.strip()]
    return keywords or None


# ---------------------------------------------------------------------------
# --find-topic 辅助命令
# ---------------------------------------------------------------------------
def find_topic(query: str) -> int:
    """打印与 ``query`` 匹配的 OpenAlex 语义主题，供手工锁定 config.TOPICS。"""
    topics = openalex_client.resolve_topics(query, limit=10)
    if not topics:
        print(f"\n没有找到与 {query!r} 匹配的主题。试试更通用的英文短语。\n", file=sys.stderr)
        return 2
    print(f"\n与 {query!r} 匹配的候选语义主题（按 OpenAlex 相关性排序，第一个最贴切）：\n")
    for name, topic_id in topics.items():
        print(f"  {topic_id}  {name}")
    print(
        "\n想锁定某个主题（而不是每次自动解析），在 src/config.py 里写：\n"
        '  TOPICS = {"<主题名称>": "<id>"}\n'
    )
    return 0


# ---------------------------------------------------------------------------
# --show-config 辅助命令
# ---------------------------------------------------------------------------
def show_config(args: argparse.Namespace) -> int:
    """离线打印「这一轮靠什么召回、靠什么打分、靠什么排序」，不联网、不发邮件。

    为什么需要它：默认 topic 模式下 USER_KEYWORDS 不参与召回，
    改完它跑一遍会发现候选量毫无变化。这个命令把各层的分工直接摊开，
    免得每次都要靠猜或者等一次完整的联网运行。
    """
    mode = args.retrieval_mode
    topics = _select_topics(args)
    multi = len(topics) > 1

    print()
    print("文献推送 · 生效配置（离线查看，未联网）")
    print("-" * 68)
    print(
        f"  主题            {len(topics)} 个"
        f"（来源：{'RESEARCH_TOPICS' if config.RESEARCH_TOPICS else '单方向 RESEARCH_FIELD 等'}，"
        "每个主题一封邮件、各记各的已推送记录）"
    )
    for index, topic in enumerate(topics, start=1):
        print(f"  {index}. {topic.name}")
        print(f"     邮件标题    {topic.email_title}")
        print(f"     TOPIC_QUERY {topic.topic_query!r}")
        print(f"     TOPICS      {topic.topics or '（空 → 按 TOPIC_QUERY 自动解析）'}")
        print(
            f"     search_terms {list(topic.search_terms) or '（空 → 退回用 keywords 召回）'}"
            "   ← 第 1 层「能搜到什么」"
        )
        print(
            f"     keywords    {list(topic.keywords) or '（空）'}"
            "   ← 第 2 层「给 AI 看的语义线索」"
        )
        print(f"     补充说明    {topic.description.strip() or '（未设置）'}")
        print(f"     主题加分    {content_rules.describe_bonuses(topic)}")
        print(f"     主题剔除    {content_rules.describe_excludes(topic)}")
        print(f"     去重分区键  {topic.key}")
    print(f"  期刊            {len(ISSN_FILTER.split('|'))} 本")
    try:
        active = sources.enabled_sources(getattr(args, "sources", None))
        source_text = " + ".join(sources.source_label(name) for name in active)
    except Exception as exc:  # noqa: BLE001 - 配置写错也要能把其它信息打出来
        source_text = f"⚠️ {exc}"
    print(f"  数据源          {source_text}   ← 每轮全部查询后按 DOI 合并")
    print(
        f"  时间窗          首次 {LOOKBACK_DAYS_FIRST_RUN} 天 / 之后 {LOOKBACK_DAYS} 天"
        f"（最多拉取 {MAX_WORKS_FETCH} 篇）"
    )
    print(f"  AI 入选线       ≥ {AI_THRESHOLD} 分，单封最多展示 {MAX_EMAIL_ITEMS} 篇")
    print(f"  排序规则        {ranking.describe()}")
    print(
        "  期刊加成        "
        + " ｜ ".join(f"{tier} +{bonus}" for tier, bonus in ranking.tiers())
        + " ｜ 其它 +0"
    )
    print(f"  内容加分（全局）{content_rules.describe_bonuses()}")
    print(f"  剔除规则（全局）{content_rules.describe_excludes()}（只看标题）")
    print(f"  本轮检索模式    {mode}")
    print("-" * 68)
    for index, topic in enumerate(topics, start=1):
        if multi:
            print(f"  ── 主题「{topic.name}」（{index}/{len(topics)}）")
        for label, detail in relevance_plan(mode, topic):
            print(f"  【{label}】{detail}")
    notices = config.config_notices()
    problems = config.config_warnings(mode)
    if multi:
        for topic in topics:
            problems += config.topic_warnings(mode, topic)
    if notices or problems:
        print()
        for notice in notices:
            print(f"  ℹ️  {notice}")
        for problem in problems:
            print(f"  ⚠️  {problem}")
    print("-" * 68)
    print("  想换「能搜到什么」        → keyword 模式改 RESEARCH_TOPICS[].search_terms（短词，OR 并联）")
    print("                              topic 模式改 topic_query（必须短且通用，否则解析为 0 会直接报错）")
    print("  想换「搜到的里面留下什么」 → 改 RESEARCH_TOPICS[].keywords / description")
    print("  想换「期刊权重」          → 改 JOURNAL_TIERS（顺序即权重顺序）")
    print("  想换「内容加权 / 不看什么」→ 改 BONUS_RULES / EXCLUDE_RULES，或主题的 bonuses / exclude")
    print("  想换「有哪些主题」        → 改 RESEARCH_TOPICS（空 = 回到单方向模式）")
    print("  真正解析出的主题 id 要看联网日志：python -m src.main --dry-run -v")
    print()
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _safe_filename(name: str) -> str:
    """把主题名变成安全的文件名片段（Windows 不允许 \\ / : * ? " < > |）。"""
    cleaned = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in name).strip()
    return cleaned or "topic"


def _select_topics(args: argparse.Namespace) -> list[config.ResearchTopic]:
    """根据 ``--topic`` 选出本轮要跑的主题；不传则全部。"""
    topics = config.active_research_topics()
    wanted = [str(name).strip() for name in (getattr(args, "topic", None) or []) if str(name).strip()]
    if not wanted:
        return topics

    by_name = {topic.name: topic for topic in topics}
    by_key = {topic.key: topic for topic in topics}
    picked: list[config.ResearchTopic] = []
    for name in wanted:
        topic = by_name.get(name) or by_key.get(name)
        if topic is None:
            raise RuntimeError(
                f"--topic {name!r} 不存在。当前可用主题：{'、'.join(by_name) or '（无）'}"
            )
        if topic not in picked:
            picked.append(topic)
    return picked


def _describe_pushed_state(topics: list[config.ResearchTopic]) -> str:
    """一句话说明去重库现状，含"孤儿分区"提醒。"""
    counts = dedup.topic_counts()
    text = f"去重库分区 {counts or '（空）'}"
    orphans = dedup.orphan_topic_keys([topic.key for topic in topics])
    if orphans:
        text += (
            f"；⚠️ 状态文件里有当前没在用的分区 {orphans}"
            "（旧记录仍在，但不再参与去重 → 这些主题会重新走首次运行的 30 天预热）"
        )
    return text


def run(args: argparse.Namespace) -> int:
    """多主题编排：对每个主题各跑一遍完整链路，各发一封邮件。"""
    run_date = today_str()
    mode = args.retrieval_mode
    topics = _select_topics(args)
    keywords = parse_keywords(args.keywords)

    log.info("=" * 68)
    log.info("文献推送 · 运行开始（%s）· %s 个主题", run_date, len(topics))
    for index, topic in enumerate(topics, start=1):
        log.info("【主题 %s/%s】%s（邮件标题：%s）", index, len(topics), topic.name, topic.email_title)
        for label, detail in relevance_plan(mode, topic):
            log.info("  【%s】%s", label, detail)
        log.info("  【内容】%s", content_rules.summary(topic))
    log.info("【排序】%s", ranking.describe())
    try:
        _active_sources = sources.enabled_sources(getattr(args, "sources", None))
        log.info(
            "【数据源】%s（每轮全部查询后按 DOI 合并；单源失败不影响其余源）",
            " + ".join(sources.source_label(name) for name in _active_sources),
        )
    except Exception as exc:  # noqa: BLE001 - 下面 fetch_works 会给出更完整的报错
        log.warning("【数据源】配置有问题：%s", exc)
    if keywords:
        if len(topics) > 1:
            log.warning("【覆盖】--keywords 在多主题模式下会被忽略（每个主题用自己的 keywords）")
            keywords = None
        else:
            log.info(
                "【覆盖】--keywords 生效：AI 打分改用 %s（本轮该主题的 keywords 不参与打分）",
                "、".join(keywords),
            )
    log.info("【模式】%s", "DRY-RUN（不发邮件）" if args.dry_run else "正式运行")
    log.info("【状态】%s", _describe_pushed_state(topics))
    log.info("=" * 68)

    for notice in config.config_notices():
        log.info("配置说明：%s", notice)
    for problem in config.config_warnings(mode):
        log.warning("配置提醒：%s", problem)
    if len(topics) > 1:
        # 单主题时上面那条已经覆盖了主题级提醒，多主题才需要逐主题补
        for topic in topics:
            for problem in config.topic_warnings(mode, topic):
                log.warning("配置提醒（%s）：%s", topic.name, problem)

    # ---- 1. 配置校验（快速失败，只需校验一次）----
    validate_env(require_ai=not args.no_ai, require_mail=not args.dry_run)

    recipients = (
        [addr.strip() for addr in args.to.replace(";", ",").split(",") if addr.strip()]
        if args.to
        else mail_recipients()
    )

    stats: list[dict] = []
    failures: list[str] = []
    for index, topic in enumerate(topics, start=1):
        try:
            stats.append(
                run_topic(topic, args, run_date, keywords, recipients, index=index, total=len(topics))
            )
        except Exception as exc:  # 一个主题挂掉不该连累其它主题
            log.error("主题「%s」运行失败：%s", topic.name, exc)
            log.debug("详细堆栈：\n%s", traceback.format_exc())
            failures.append(topic.name)

    _log_overall(stats, failures, len(topics), dry_run=args.dry_run)
    if failures:
        raise RuntimeError(
            f"{len(failures)}/{len(topics)} 个主题运行失败：{'、'.join(failures)}"
            "（其余主题已处理完毕，详见上面的日志）"
        )
    return 0


def run_topic(
    topic: config.ResearchTopic,
    args: argparse.Namespace,
    run_date: str,
    keywords: list[str] | None,
    recipients: list[str],
    *,
    index: int = 1,
    total: int = 1,
) -> dict:
    """跑一个主题的完整链路：检索 → 去重 → 摘要 → AI 打分 → 加权排序 → 发信 → 回写状态。"""
    mode = args.retrieval_mode
    prefix = f"[{index}/{total}] " if total > 1 else ""
    if total > 1:
        log.info("-" * 68)
        log.info("%s主题：%s", prefix, topic.name)

    # ---- 2. 时间窗（按主题各自的去重记录判定首次运行）----
    first_run = args.force_first_run or dedup.is_first_run(topic.key)
    if args.lookback_days is not None:
        lookback_days = args.lookback_days
    else:
        lookback_days = LOOKBACK_DAYS_FIRST_RUN if first_run else LOOKBACK_DAYS
    log.info(
        "%s时间窗：近 %s 天（%s）",
        prefix,
        lookback_days,
        "首次运行预热" if first_run else "常规滚动",
    )

    # ---- 3. 检索（第 1 层筛选：多数据源并集 → 按 DOI 合并）----
    # 见 src/sources/__init__.py：每轮把所有启用的源**都问一遍**再合并，
    # 而不是「主源挂了才切备用」—— 后者会把源故障伪装成「本周没有新论文」，
    # 而那正是这个项目吃过的最大的亏。
    fetched = sources.fetch_works(
        keywords,
        lookback_days,
        max_works=args.max_fetch,
        mode=mode,
        topic=topic,
        sources=getattr(args, "sources", None),
    )
    works = fetched.works
    total_candidates = len(works)
    log.info("%s检索汇总：%s", prefix, fetched.summary())
    for notice in fetched.notices():
        log.warning("%s%s", prefix, notice)
    if not total_candidates:
        log.warning("%s所有数据源都没返回候选文献（时间窗 %s 天）", prefix, lookback_days)

    # ---- 4. 去重（按主题各自的记录）----
    fresh = dedup.filter_new(works, topic_key=topic.key)
    after_dedup = len(fresh)

    # ---- 5. 摘要回退 ----
    if fresh:
        fresh = abstract_source.enrich_abstracts(fresh)

    # ---- 5.5 内容规则剔除（不看电解液工程 / 隔膜改性）----
    # 放在 AI 之前：被剔除的文献不进 AI 打分（省钱），也不进邮件。
    excluded: list[dict] = []
    if fresh:
        fresh, excluded = content_rules.partition_excluded(fresh, topic)
        content_rules.log_excluded(excluded, topic, prefix=prefix)

    # ---- 6. AI 打分（第 2 层筛选：语义相关性）----
    ai_failed: list[dict] = []
    rejected: list[dict] = []
    if not fresh:
        selected: list[dict] = []
    elif args.no_ai:
        log.warning("%s--no-ai 已启用：跳过 AI 打分，全部 %s 篇直接进入结果", prefix, len(fresh))
        for work in fresh:
            work.update(ai_score=0, ai_takeaway="（跳过 AI）", ai_reason="--no-ai 模式", ai_error=False)
        selected = list(fresh)
    else:
        selected, ai_failed, rejected = ai_matcher.evaluate_works(
            fresh, keywords=keywords, threshold=args.threshold, topic=topic
        )

    # ---- 6.5 加权排序：最终分 = AI 分 + 期刊档次加成 + 内容规则加成 ----
    selected = ranking.rank(selected, topic)
    if selected:
        top = selected[0]
        log.info(
            "%s加权排序完成，首位：%s 分（%s）",
            prefix,
            top.get("final_score"),
            ranking.breakdown(top),
        )

    # ---- 7. 渲染 ----
    # 数据源与主题都写进页头元信息：用户一眼能看出这轮的候选是几个源凑出来的。
    meta_extras = []
    if total > 1:
        meta_extras.append(f"主题：{topic.name}")
    meta_extras.append(f"数据源：{fetched.summary()}")

    html_body, plain_body = mailer.build_html(
        selected,
        run_date,
        lookback_days=lookback_days,
        first_run=first_run,
        total_candidates=total_candidates,
        after_dedup=after_dedup,
        excluded=len(excluded),
        ai_failed=len(ai_failed),
        title=topic.email_title,
        max_items=args.max_items,
        extra_meta=" ｜ ".join(meta_extras),
        extra_notices=fetched.notices(),
    )

    stats = {
        "name": topic.name,
        "candidates": total_candidates,
        "after_dedup": after_dedup,
        "excluded": len(excluded),
        "selected": len(selected),
        "shown": min(len(selected), args.max_items),
        "mailed": 0,
        "sources": fetched.summary(),
    }

    # ---- 8. 输出 ----
    if args.dry_run:
        suffix = f"-{_safe_filename(topic.name)}" if total > 1 else ""
        path = mailer.save_preview(html_body, run_date, OUTBOX_DIR, suffix=suffix)
        log.info("%s[dry-run] 未发送邮件、未修改去重状态；预览文件：%s", prefix, path)
        _log_summary(
            topic.name,
            total_candidates,
            after_dedup,
            len(selected),
            len(ai_failed),
            dry_run=True,
            excluded=len(excluded),
        )
        return stats

    subject = mailer.build_subject(selected, run_date, topic.email_title)

    # 发送成功后才回写状态（关键：避免邮件失败导致文献永久丢失）
    mailer.send_mail(subject, html_body, plain_body, recipients=recipients)
    log.info("%s邮件发送成功：%s", prefix, subject)
    stats["mailed"] = 1

    shown = selected[: args.max_items]

    # ------------------------------------------------------------------
    # 回写"已读"标记的规则（按主题各自的记录）
    # ------------------------------------------------------------------
    # 主题检索下候选近 200 篇，而邮件只发 20 篇。若只记录展示过的那 20 篇，
    # 剩下 170+ 篇下周会被原封不动地重新打分一遍 —— 每周白烧 5 倍 AI 费用。
    #
    # 记录：实际展示过的 + AI 明确判定不相关（低于阈值）**且当时有摘要**的
    #      —— 分数确定，重打是纯浪费。
    #
    # 故意**不**记录：
    #   * 通过阈值但被 20 篇上限挤掉的"备选" —— 下次还有机会入选
    #   * AI 调用失败 —— 分数不可信，必须下轮重试
    #   * 缺摘要被判不相关 —— 出版商索引有延迟，下周摘要可能才出现，
    #     结论可能完全反转，不能让"已读"把它永久锁死
    to_mark = shown + [w for w in rejected if w.get("abstract")]
    if to_mark:
        dedup.mark_pushed(to_mark, run_date=run_date, topic_key=topic.key)
    else:
        # 心跳邮件场景：也要更新 last_run，但不动 DOIs
        dedup.save_state([], run_date=run_date, topic_key=topic.key)

    log.info(
        "%s状态回写：%s 篇标记已读（展示 %s + 已判定不相关 %s）；未标记的备选 %s 篇下轮会重新评估",
        prefix,
        len(to_mark),
        len(shown),
        len(to_mark) - len(shown),
        max(0, len(selected) - len(shown)),
    )
    _log_summary(
        topic.name,
        total_candidates,
        after_dedup,
        len(shown),
        len(ai_failed),
        dry_run=False,
        excluded=len(excluded),
    )
    return stats


def _log_summary(
    topic_name: str,
    candidates: int,
    after_dedup: int,
    selected: int,
    ai_failed: int,
    dry_run: bool,
    excluded: int = 0,
) -> None:
    log.info("-" * 68)
    log.info(
        "运行摘要（%s）：候选 %s 篇 → 去重后 %s 篇%s → 入选 %s 篇%s%s",
        topic_name,
        candidates,
        after_dedup,
        f"（规则剔除 {excluded} 篇）" if excluded else "",
        selected,
        f"（AI 失败 {ai_failed} 篇）" if ai_failed else "",
        "（dry-run，未发送）" if dry_run else "",
    )
    log.info("-" * 68)


def _log_overall(stats: list[dict], failures: list[str], total_topics: int, dry_run: bool) -> None:
    """多主题的总账。放在最后一行，方便 Actions 日志一眼看到全貌。"""
    log.info("=" * 68)
    for item in stats:
        log.info(
            "【%s】候选 %s → 去重后 %s%s → 入选 %s%s",
            item["name"],
            item["candidates"],
            item["after_dedup"],
            f"（规则剔除 {item['excluded']} 篇）" if item.get("excluded") else "",
            item["selected"],
            "" if dry_run else f" → 已发邮件 {item['mailed']} 封",
        )
    sent = sum(item["mailed"] for item in stats)
    log.info(
        "全部完成：%s 个主题%s%s",
        total_topics,
        "（dry-run，未发送任何邮件）" if dry_run else f"，已发送 {sent} 封邮件",
        f"，失败 {len(failures)} 个：{'、'.join(failures)}" if failures else "",
    )
    log.info("=" * 68)


def _force_utf8_streams() -> None:
    """把 stdout/stderr 切到 UTF-8。

    只在**本地把输出重定向到文件**时才有影响：Windows 控制台默认 GBK，
    而 ``--show-config`` 会打印 ℹ️ ⚠️ 这类符号，重定向后 ``print`` 会直接
    ``UnicodeEncodeError`` 崩掉（GitHub Actions 上是 UTF-8，不受影响）。
    拿不到 ``reconfigure``（例如测试里的 ``io.StringIO``）就安静跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - 环境相关
            pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _force_utf8_streams()
    setup_logging(verbose=args.verbose)

    try:
        if args.show_config:
            return show_config(args)

        if args.find_topic:
            return find_topic(args.find_topic)

        return run(args)
    except RuntimeError as exc:
        log.error("运行失败：%s", exc)
        return 2
    except Exception:
        log.error("未预期的异常：\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
