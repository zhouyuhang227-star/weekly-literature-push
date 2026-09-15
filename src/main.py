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

from . import abstract_source, ai_matcher, dedup, mailer, openalex_client
from .config import (
    AI_THRESHOLD,
    EMAIL_TITLE,
    ISSN_FILTER,
    LOOKBACK_DAYS,
    LOOKBACK_DAYS_FIRST_RUN,
    MAX_EMAIL_ITEMS,
    MAX_WORKS_FETCH,
    OUTBOX_DIR,
    RESEARCH_DESCRIPTION,
    RESEARCH_FIELD,
    RETRIEVAL_MODE,
    TOPICS,
    TOPIC_QUERY,
    USER_KEYWORDS,
    config_warnings,
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
            f"文献自动推送机器人：OpenAlex 检索 → AI 相关性筛选 → 邮件周报"
            f"（当前研究方向：{RESEARCH_FIELD}）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m src.main --dry-run --verbose\n"
            "  python -m src.main --lookback-days 90 --max-items 50\n"
            "  python -m src.main --no-ai --to me@example.com\n"
            "  python -m src.main --find-topic \"perovskite solar cell\"\n"
            "\n换研究方向只需改 src/config.py 的「研究方向」段落。\n"
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
        "--show-config",
        action="store_true",
        help=(
            "不跑主流程，只打印「这一轮靠什么召回、靠什么打分」然后退出（纯离线，不联网）。"
            "改完 config.py 拿不准改动生效在哪一层时先跑它"
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
    """离线打印「这一轮靠什么召回、靠什么打分」，不联网、不发邮件。

    为什么需要它：默认 topic 模式下 USER_KEYWORDS 不参与召回，
    改完它跑一遍会发现候选量毫无变化。这个命令把两层的分工直接摊开，
    免得每次都要靠猜或者等一次完整的联网运行。
    """
    mode = args.retrieval_mode
    print()
    print(f"{EMAIL_TITLE} · 生效配置（离线查看，未联网）")
    print("-" * 68)
    print(f"  研究方向        {RESEARCH_FIELD}")
    print(f"  补充说明        {RESEARCH_DESCRIPTION.strip() or '（未设置）'}")
    print(f"  USER_KEYWORDS   {USER_KEYWORDS or '（空）'}")
    print(f"  TOPIC_QUERY     {TOPIC_QUERY!r}")
    print(f"  TOPICS          {TOPICS or '（空 → 按 TOPIC_QUERY 自动解析）'}")
    print(f"  期刊            {len(ISSN_FILTER.split('|'))} 本")
    print(
        f"  时间窗          首次 {LOOKBACK_DAYS_FIRST_RUN} 天 / 之后 {LOOKBACK_DAYS} 天"
        f"（最多拉取 {MAX_WORKS_FETCH} 篇）"
    )
    print(f"  AI 入选线       ≥ {AI_THRESHOLD} 分，单封最多展示 {MAX_EMAIL_ITEMS} 篇")
    print(f"  本轮检索模式    {mode}")
    print("-" * 68)
    for label, detail in relevance_plan(mode):
        print(f"  【{label}】{detail}")
    problems = config_warnings(mode)
    if problems:
        print()
        for problem in problems:
            print(f"  ⚠️  {problem}")
    print("-" * 68)
    print("  想换「能搜到什么」        → 改 RETRIEVAL_MODE / TOPIC_QUERY")
    print("  想换「搜到的里面留下什么」 → 改 RESEARCH_FIELD / RESEARCH_DESCRIPTION / USER_KEYWORDS")
    print("  真正解析出的主题 id 要看联网日志：python -m src.main --dry-run -v")
    print()
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    run_date = today_str()
    keywords = parse_keywords(args.keywords)

    log.info("=" * 68)
    log.info("%s · 运行开始（%s）", EMAIL_TITLE, run_date)
    # 把「两把旋钮」的分工直接打出来：topic 模式下改 USER_KEYWORDS 不会改变候选量，
    # 这是本项目最容易让人误判成 bug 的地方，所以每轮都明说一下。
    for label, detail in relevance_plan(args.retrieval_mode):
        log.info("【%s】%s", label, detail)
    if keywords:
        log.info(
            "【覆盖】--keywords 生效：AI 打分改用 %s（本轮 config.USER_KEYWORDS 不参与打分）",
            "、".join(keywords),
        )
    log.info("【模式】%s", "DRY-RUN（不发邮件）" if args.dry_run else "正式运行")
    log.info("=" * 68)
    for problem in config_warnings(args.retrieval_mode):
        log.warning("配置提醒：%s", problem)

    # ---- 1. 配置校验（快速失败）----
    validate_env(require_ai=not args.no_ai, require_mail=not args.dry_run)

    # ---- 2. 时间窗 ----
    first_run = args.force_first_run or dedup.is_first_run()
    if args.lookback_days is not None:
        lookback_days = args.lookback_days
    else:
        lookback_days = LOOKBACK_DAYS_FIRST_RUN if first_run else LOOKBACK_DAYS
    log.info("时间窗：近 %s 天（%s）", lookback_days, "首次运行预热" if first_run else "常规滚动")

    # ---- 3. 检索（第 1 层筛选：主题分类 / 字面关键词）----
    works = openalex_client.fetch_works(
        keywords,
        lookback_days,
        max_works=args.max_fetch,
        mode=args.retrieval_mode,
    )
    total_candidates = len(works)
    if not total_candidates:
        log.warning("OpenAlex 未返回任何候选文献（时间窗 %s 天）", lookback_days)

    # ---- 4. 去重 ----
    fresh = dedup.filter_new(works)
    after_dedup = len(fresh)

    # ---- 5. 摘要回退 ----
    if fresh:
        fresh = abstract_source.enrich_abstracts(fresh)

    # ---- 6. AI 打分（第 2 层筛选：语义相关性）----
    ai_failed: list[dict] = []
    rejected: list[dict] = []
    if not fresh:
        selected: list[dict] = []
    elif args.no_ai:
        log.warning("--no-ai 已启用：跳过 AI 打分，全部 %s 篇直接进入结果", len(fresh))
        for work in fresh:
            work.update(ai_score=0, ai_takeaway="（跳过 AI）", ai_reason="--no-ai 模式", ai_error=False)
        selected = sorted(fresh, key=lambda w: w.get("pub_date") or "", reverse=True)
    else:
        selected, ai_failed, rejected = ai_matcher.evaluate_works(
            fresh, keywords=keywords, threshold=args.threshold
        )

    # ---- 7. 渲染 ----
    html_body, plain_body = mailer.build_html(
        selected,
        run_date,
        lookback_days=lookback_days,
        first_run=first_run,
        total_candidates=total_candidates,
        after_dedup=after_dedup,
        ai_failed=len(ai_failed),
    )

    # ---- 8. 输出 ----
    if args.dry_run:
        path = mailer.save_preview(html_body, run_date, OUTBOX_DIR)
        log.info("[dry-run] 未发送邮件、未修改去重状态")
        log.info("[dry-run] 预览文件：%s", path)
        _log_summary(total_candidates, after_dedup, len(selected), len(ai_failed), dry_run=True)
        return 0

    recipients = [addr.strip() for addr in args.to.replace(";", ",").split(",") if addr.strip()] if args.to else mail_recipients()
    subject = mailer.build_subject(selected, run_date)

    # 发送成功后才回写状态（关键：避免邮件失败导致文献永久丢失）
    mailer.send_mail(subject, html_body, plain_body, recipients=recipients)
    log.info("邮件发送成功：%s", subject)

    shown = selected[: args.max_items]

    # ------------------------------------------------------------------
    # 回写"已读"标记的规则
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
        dedup.mark_pushed(to_mark, run_date=run_date)
    else:
        # 心跳邮件场景：也要更新 last_run，但不动 DOIs
        dedup.save_state([], run_date=run_date)

    log.info(
        "状态回写：%s 篇标记已读（展示 %s + 已判定不相关 %s）；未标记的备选 %s 篇下轮会重新评估",
        len(to_mark),
        len(shown),
        len(to_mark) - len(shown),
        max(0, len(selected) - len(shown)),
    )
    _log_summary(total_candidates, after_dedup, len(shown), len(ai_failed), dry_run=False)
    return 0


def _log_summary(candidates: int, after_dedup: int, selected: int, ai_failed: int, dry_run: bool) -> None:
    log.info("-" * 68)
    log.info(
        "运行摘要：候选 %s 篇 → 去重后 %s 篇 → 入选 %s 篇%s%s",
        candidates,
        after_dedup,
        selected,
        f"（AI 失败 {ai_failed} 篇）" if ai_failed else "",
        "（dry-run，未发送）" if dry_run else "",
    )
    log.info("-" * 68)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)

    if args.show_config:
        return show_config(args)

    if args.find_topic:
        return find_topic(args.find_topic)

    try:
        return run(args)
    except RuntimeError as exc:
        log.error("运行失败：%s", exc)
        return 2
    except Exception:
        log.error("未预期的异常：\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
