"""核心纯函数自测（仅用标准库 unittest，不引入额外依赖）。

运行：

    python -m unittest discover -s tests -v

覆盖的是最容易出错、且不需要网络/密钥的逻辑：
DOI 规范化、倒排摘要还原、AI 返回解析、去重状态读写、HTML 转义。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import abstract_source, ai_matcher, config, content_rules, dedup, mailer  # noqa: E402
from src import main as main_module  # noqa: E402
from src import openalex_client, ranking  # noqa: E402
from src.openalex_client import (  # noqa: E402
    build_filter,
    build_keyword_query,
    normalize_doi,
    parse_work,
    reconstruct_abstract,
)


class TestDoiNormalize(unittest.TestCase):
    def test_strips_https_prefix(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.1038/S41560-026-02133-3"),
            "10.1038/s41560-026-02133-3",
        )

    def test_strips_dx_prefix(self):
        self.assertEqual(normalize_doi("http://dx.doi.org/10.1016/j.joule.2026.1"), "10.1016/j.joule.2026.1")

    def test_handles_empty(self):
        self.assertEqual(normalize_doi(None), "")
        self.assertEqual(normalize_doi(""), "")

    def test_no_prefix_passthrough(self):
        self.assertEqual(normalize_doi("10.1002/adma.75004"), "10.1002/adma.75004")


class TestReconstructAbstract(unittest.TestCase):
    def test_rebuilds_word_order(self):
        inverted = {"Solid": [0], "state": [1], "batteries": [2], "work": [3]}
        self.assertEqual(reconstruct_abstract(inverted), "Solid state batteries work")

    def test_handles_repeated_words(self):
        inverted = {"a": [0, 2], "b": [1]}
        self.assertEqual(reconstruct_abstract(inverted), "a b a")

    def test_none_returns_empty(self):
        self.assertEqual(reconstruct_abstract(None), "")
        self.assertEqual(reconstruct_abstract({}), "")


class TestQueryBuilding(unittest.TestCase):
    """过滤器拼接。主题 id 在这里固定成假值，避免测试联网。"""

    def setUp(self):
        self._saved_topics = config.TOPICS
        self._saved_cache = config.TOPICS_CACHE_FILE
        config.TOPICS = {"Test Topic": "T9999"}

    def tearDown(self):
        config.TOPICS = self._saved_topics
        config.TOPICS_CACHE_FILE = self._saved_cache

    def test_phrases_are_quoted(self):
        self.assertEqual(
            build_keyword_query(["solid-state battery", "lithium dendrite"]),
            '"solid-state battery" OR "lithium dendrite"',
        )

    def test_blank_keywords_dropped(self):
        self.assertEqual(build_keyword_query(["  ", "x"]), '"x"')

    def test_filter_uses_title_and_abstract(self):
        from datetime import date

        value = build_filter(date(2026, 8, 16), ["solid-state battery"], mode="keyword")
        self.assertIn("title_and_abstract.search:", value)
        self.assertIn("from_publication_date:2026-08-16", value)
        self.assertIn("type:article", value)
        self.assertIn("is_retracted:false", value)
        self.assertIn("is_paratext:false", value)
        # 关键回归：不能落在全文检索上
        self.assertNotIn("fulltext", value)
        # keyword 模式不应带主题条件
        self.assertNotIn("topics.id", value)

    # ------------------------------------------------------------------
    # 主题检索模式（默认）
    # ------------------------------------------------------------------
    def test_topic_mode_uses_topics_id(self):
        """默认模式必须走语义主题分类，而不是字面关键词。"""
        from datetime import date

        value = build_filter(date(2026, 8, 16), ["solid-state battery"], mode="topic")
        self.assertIn("topics.id:", value)
        self.assertIn(openalex_client.topic_filter_value(), value)
        # 主题模式下不应再叠加关键词条件（否则又退回字面匹配）
        self.assertNotIn("title_and_abstract.search", value)

    def test_topic_mode_still_has_issn_and_date(self):
        from datetime import date

        value = build_filter(date(2026, 8, 16), None, mode="topic")
        self.assertIn("primary_location.source.issn:", value)
        self.assertIn("from_publication_date:2026-08-16", value)

    def test_both_mode_combines(self):
        from datetime import date

        value = build_filter(date(2026, 8, 16), ["lithium dendrite"], mode="both")
        self.assertIn("topics.id:", value)
        self.assertIn("title_and_abstract.search:", value)

    def test_default_mode_is_keyword(self):
        """默认参数应当是 keyword —— 防止有人悄悄改回 topic。

        历史教训：默认曾经是 topic，但 OpenAlex 的主题分类粒度太粗。
        实测 ``"sodium-ion battery"`` / ``"lithium-rich"`` 这类短语在
        ``/topics?search=`` 里返回 **0 条**，于是每个主题都静默退化成
        「全部期刊近 N 天」，两个主题拿到**同一批**无关论文，
        最后一边报 0 篇、一边报 4 篇（看起来像「规则太严」，实际是没搜到）。
        所以默认改成 keyword，并要求 search_terms 写能在标题/摘要里逐字出现的短词。
        """
        from datetime import date

        self.assertEqual(config.RETRIEVAL_MODE, "keyword")
        value = build_filter(date(2026, 8, 16), ["x"])
        self.assertIn("title_and_abstract.search:", value)
        self.assertNotIn("topics.id:", value)


class TestRecallTermsAreShortAndLiteral(unittest.TestCase):
    """召回层（search_terms）与打分层（keywords）必须分开，且缺失时必须吵。

    锁的是三件事：
      1. 进 OpenAlex 的是 search_terms，不是给 AI 看的长句子 keywords；
        没写 search_terms 的老主题要能退回 keywords（不能静默召回为空）；
      2. 召回条件为空时 build_filter **抛异常**，不能静默搜全库
         （真实事故：两个主题各拿到同一批 300 篇无关论文，一个报 0 篇、一个报 4 篇）；
      3. 写得太长/带括号斜杠的 search_terms 会被 config 校验点名。
    """

    def test_search_terms_drive_recall_and_keywords_stay_out_of_the_query(self):
        from datetime import date

        topic = config.ResearchTopic(
            name="富锂锰正极",
            search_terms=["li-rich", "oxygen redox"],
            keywords=["lithium-rich layered oxide (LRLO / LMR) —— 给 AI 看的语义线索"],
        )
        value = build_filter(date(2026, 8, 16), None, mode="keyword", topic=topic)
        self.assertIn('"li-rich" OR "oxygen redox"', value)
        self.assertNotIn("LRLO", value)

    def test_missing_search_terms_fall_back_to_keywords(self):
        """老配置（只有 keywords）行为不变：这才是「向后兼容」的含义。"""
        from datetime import date

        topic = config.ResearchTopic(name="老主题", keywords=["solid-state battery"])
        value = build_filter(date(2026, 8, 16), None, mode="keyword", topic=topic)
        self.assertIn('"solid-state battery"', value)

    def test_empty_search_terms_raises_instead_of_searching_everything(self):
        from datetime import date

        topic = config.ResearchTopic(name="空主题")
        with self.assertRaises(RuntimeError) as ctx:
            build_filter(date(2026, 8, 16), None, mode="keyword", topic=topic)
        self.assertIn("空主题", str(ctx.exception))
        self.assertIn("全库检索", str(ctx.exception))

    def test_unresolved_topic_ids_raise_in_topic_mode(self):
        """topic 模式解析到 0 个 id 时必须抛错 —— 静默退化的代价是「绿着跑错」。"""
        from datetime import date

        with patch.object(openalex_client, "topic_filter_value", return_value=""):
            with self.assertRaises(RuntimeError) as ctx:
                build_filter(date(2026, 8, 16), ["x"], mode="topic", topic=None)
        self.assertIn("全库检索", str(ctx.exception))

    def test_both_mode_degrades_loudly_when_topic_ids_missing(self):
        """both 模式有字面词兜底，所以不抛错，但必须留下 WARNING。"""
        from datetime import date

        with patch.object(openalex_client, "topic_filter_value", return_value=""):
            with self.assertLogs(openalex_client.log, level="WARNING") as captured:
                value = build_filter(date(2026, 8, 16), ["li-rich"], mode="both")
        self.assertNotIn("topics.id", value)
        self.assertIn("title_and_abstract.search:", value)
        self.assertTrue(any("只走字面关键词召回" in line for line in captured.output))

    def test_config_flags_symbols_and_long_phrases_in_search_terms(self):
        topic = config.ResearchTopic(
            name="X",
            search_terms=["lithium-rich layered oxide (LRLO / LMR)", "one two three four five"],
            keywords=["keep"],
        )
        problems = config.topic_warnings("keyword", topic)
        self.assertTrue(any("括号或斜杠" in p for p in problems))
        self.assertTrue(any("个词" in p and "缩短" in p for p in problems))

    def test_config_flags_empty_recall_terms_in_keyword_mode(self):
        topic = config.ResearchTopic(name="X", keywords=[])
        problems = config.topic_warnings("keyword", topic)
        self.assertTrue(any("直接报错" in p for p in problems))

    def test_live_config_recall_terms_are_clean(self):
        """真实 config 的 search_terms 必须过校验，且每个主题都要有召回词。"""
        for topic in config.active_research_topics():
            self.assertTrue(topic.search_terms, f"主题「{topic.name}」没有 search_terms")
            problems = [
                p for p in config.topic_warnings("keyword", topic)
                if "search_terms" in p
            ]
            self.assertEqual(problems, [], f"主题「{topic.name}」的 search_terms 有问题：{problems}")


class TestResearchDirectionIsConfigurable(unittest.TestCase):
    """换研究方向必须只改 config，且不能留下静默用错方向的空间。

    这些测试存在的意义：一旦有人在源码里重新写死"固态电池/电池材料"，
    或者让主题 id 与关键词脱钩（改了关键词却仍检索旧主题），这里会立刻变红。
    """

    def setUp(self):
        self._saved = {
            name: getattr(config, name)
            for name in (
                "RESEARCH_FIELD",
                "RESEARCH_DESCRIPTION",
                "USER_KEYWORDS",
                # 全局排除说明是**用户配置**（会拼进 prompt），不算模板里写死的学科词，
                # 所以这里要保存/恢复，下面的"无写死示例"测试会把它清空。
                "GLOBAL_EXCLUDE_NOTE",
            )
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config, name, value)

    def test_system_prompt_follows_config(self):
        config.RESEARCH_FIELD = "钙钛矿太阳能电池"
        prompt = ai_matcher.build_system_prompt()
        self.assertIn("钙钛矿太阳能电池", prompt)
        # 不能残留任何学科写死的痕迹
        for stale in ("固态电池", "电池材料"):
            self.assertNotIn(stale, prompt)

    def test_user_prompt_includes_description_and_keywords(self):
        config.RESEARCH_FIELD = "钙钛矿太阳能电池"
        config.RESEARCH_DESCRIPTION = "只要无机钙钛矿，不含纯有机体系"
        config.USER_KEYWORDS = ["perovskite solar cell"]
        prompt = ai_matcher.build_prompt({"title": "T", "journal": "J"}, None)
        self.assertIn("钙钛矿太阳能电池", prompt)
        self.assertIn("只要无机钙钛矿", prompt)
        self.assertIn("perovskite solar cell", prompt)

    def test_user_prompt_has_no_hardcoded_score_examples(self):
        """评分档位里的"固态电解质、锂金属负极"这类示例必须已经清除。

        注意：这里也要把 ``GLOBAL_EXCLUDE_NOTE`` 清空 —— 那是用户能在 config 里改的
        配置文本（会拼进 prompt），不是模板里写死的学科词。
        """
        config.USER_KEYWORDS = ["perovskite solar cell"]
        config.GLOBAL_EXCLUDE_NOTE = ""
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        for stale in ("固态电解质", "锂金属负极", "液态电解液", "钠离子电池"):
            self.assertNotIn(stale, prompt)

    def test_global_exclude_note_reaches_the_prompt(self):
        """全局排除说明要真的进到 AI 的尺子里（不然 AI 不知道你要避开什么）。"""
        config.USER_KEYWORDS = ["x"]
        config.RESEARCH_DESCRIPTION = ""
        config.GLOBAL_EXCLUDE_NOTE = "不看隔膜改性"
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        self.assertIn("不看隔膜改性", prompt)

    def test_explicit_keywords_override_config_brief(self):
        config.RESEARCH_DESCRIPTION = "不应出现在这里"
        prompt = ai_matcher.build_prompt({"title": "T"}, ["only-this"])
        self.assertIn("only-this", prompt)
        self.assertNotIn("不应出现在这里", prompt)

    def test_email_title_follows_config(self):
        with patch.object(mailer, "EMAIL_TITLE", "钙钛矿顶刊周报"):
            self.assertEqual(mailer.build_subject([{"doi": "x"}], "2026-09-15"), "钙钛矿顶刊周报 · 2026-09-15 · 1 篇")
            self.assertEqual(mailer.build_subject([], "2026-09-15"), "钙钛矿顶刊周报 · 2026-09-15 · 本周无新文献")
            html, plain = mailer.build_html([], "2026-09-15", lookback_days=14, first_run=False)
            self.assertIn("钙钛矿顶刊周报", html)
            self.assertIn("钙钛矿顶刊周报", plain)

    def test_email_title_defaults_to_research_field(self):
        self.assertEqual(config.EMAIL_TITLE, f"{config.RESEARCH_FIELD}顶刊周报")


class TestRelevanceLayersAreExplained(unittest.TestCase):
    """「两把旋钮」必须每轮都说明白。

    锁住的是**提示文字本身**：默认 topic 模式下 USER_KEYWORDS 不参与召回，
    只改它会出现「候选量一点没变」的现象，而且不报任何错。
    一旦有人把 relevance_plan 删掉或让它不再提这件事，这里就变红 ——
    防止这个陷阱重新变得静默。
    """

    def setUp(self):
        self._saved = {
            name: getattr(config, name)
            for name in ("RESEARCH_FIELD", "RESEARCH_DESCRIPTION", "USER_KEYWORDS", "AI_THRESHOLD")
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config, name, value)

    @staticmethod
    def _detail(plan, label):
        return dict(plan)[label]

    def test_topic_mode_says_keywords_do_not_drive_recall(self):
        plan = config.relevance_plan("topic")
        self.assertEqual(
            [label for label, _ in plan],
            [config.LAYER_RECALL, config.LAYER_SCORING, config.LAYER_HINT],
        )
        recall = self._detail(plan, config.LAYER_RECALL)
        self.assertIn("topic", recall)
        self.assertIn(config.TOPIC_QUERY, recall)
        self.assertIn("USER_KEYWORDS 不参与", recall)

        # 光说"不生效"不够，必须直接给出"想让它生效该改哪里"
        hint = self._detail(plan, config.LAYER_HINT)
        self.assertIn('RETRIEVAL_MODE = "both"', hint)
        self.assertIn("TOPIC_QUERY", hint)

    def test_keyword_mode_says_keywords_do_drive_recall(self):
        config.USER_KEYWORDS = ["perovskite solar cell"]
        recall = self._detail(config.relevance_plan("keyword"), config.LAYER_RECALL)
        self.assertIn("keyword", recall)
        self.assertIn("perovskite solar cell", recall)
        self.assertNotIn("不参与", recall)

    def test_both_mode_mentions_both_recall_sources(self):
        recall = self._detail(config.relevance_plan("both"), config.LAYER_RECALL)
        self.assertIn("topics.id", recall)
        self.assertIn("TOPIC_QUERY", recall)

    def test_scoring_layer_follows_config(self):
        config.RESEARCH_FIELD = "钙钛矿太阳能电池"
        config.AI_THRESHOLD = 77
        scoring = self._detail(config.relevance_plan("topic"), config.LAYER_SCORING)
        self.assertIn("钙钛矿太阳能电池", scoring)
        self.assertIn("77", scoring)

    def test_unknown_mode_is_reported_not_crashed(self):
        recall = self._detail(config.relevance_plan("nope"), config.LAYER_RECALL)
        self.assertIn("nope", recall)

    def test_plan_is_recomputed_from_current_config(self):
        """必须是「当前」配置，不能在 import 时把值算死。"""
        config.USER_KEYWORDS = ["aaa"]
        first = self._detail(config.relevance_plan("keyword"), config.LAYER_RECALL)
        config.USER_KEYWORDS = ["bbb"]
        second = self._detail(config.relevance_plan("keyword"), config.LAYER_RECALL)
        self.assertIn("aaa", first)
        self.assertIn("bbb", second)
        self.assertNotIn("bbb", first)


class TestConfigWarnings(unittest.TestCase):
    """只报真正配错的组合，不狼来了。

    这些用例针对的是【单方向模式】，所以先把 RESEARCH_TOPICS 清空 —— 
    默认 config 已开启多主题，不清空的话那些常数本就不参与，也就无从报错。
    """

    def setUp(self):
        self._saved = {
            name: getattr(config, name)
            for name in ("USER_KEYWORDS", "TOPIC_QUERY", "TOPICS", "RESEARCH_TOPICS")
        }
        config.RESEARCH_TOPICS = []

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config, name, value)

    def test_healthy_config_is_silent(self):
        # 默认配置（topic + 有主题 + 有关键词）不该在每周日志里刷警告
        self.assertEqual(config.config_warnings("topic"), [])
        self.assertEqual(config.config_warnings("both"), [])

    def test_empty_keywords_warns_because_ai_loses_its_ruler(self):
        config.USER_KEYWORDS = []
        problems = config.config_warnings("topic")
        self.assertTrue(any("USER_KEYWORDS 为空" in p for p in problems))

    def test_topic_mode_without_any_topic_source_says_it_will_fail(self):
        """没主题可解析时必须说清楚「会直接报错」，而不是「退化成全库检索」。

        因为现在真的会报错（build_filter 抛 RuntimeError），
        提醒文案与行为不一致的话，看日志的人会被引到错的方向。
        """
        config.TOPICS = {}
        config.TOPIC_QUERY = "   "
        problems = config.config_warnings("topic")
        self.assertTrue(any("直接报错" in p and "全库检索" in p for p in problems))

        # 手工锁定了 TOPICS 后就不该再报
        config.TOPICS = {"X": "T1"}
        self.assertEqual(config.config_warnings("topic"), [])

    def test_bad_mode_warns(self):
        self.assertTrue(any("不是合法值" in p for p in config.config_warnings("typo")))


class TestShowConfigCommand(unittest.TestCase):
    """--show-config 必须离线、不跑主流程，并把两层分工讲清楚。"""

    def setUp(self):
        # 这些断言按「单方向」写法组织，因此先关掉多主题（默认已开启）。
        self._saved_topics = config.RESEARCH_TOPICS
        config.RESEARCH_TOPICS = []

    def tearDown(self):
        config.RESEARCH_TOPICS = self._saved_topics

    def test_show_config_prints_layers_and_returns_zero(self):
        from src import main as main_module

        args = main_module.build_parser().parse_args(["--show-config"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main_module.show_config(args)

        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn(config.LAYER_RECALL, text)
        self.assertIn(config.LAYER_SCORING, text)
        self.assertIn(config.LAYER_HINT, text)
        # 必须回显真实的 TOPIC_QUERY，否则看了也不知道在搜什么
        self.assertIn(config.TOPIC_QUERY, text)

    def test_show_config_honours_retrieval_mode_flag(self):
        from src import main as main_module

        args = main_module.build_parser().parse_args(["--show-config", "--retrieval-mode", "both"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main_module.show_config(args)

        text = buffer.getvalue()
        self.assertIn("both", text)
        # both 模式不该再出现「USER_KEYWORDS 不参与召回」
        self.assertNotIn("USER_KEYWORDS 不参与", text)

    def test_main_routes_show_config_before_running_pipeline(self):
        from src import main as main_module

        buffer = io.StringIO()
        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "run", side_effect=AssertionError("不该进入主流程")
        ):
            with contextlib.redirect_stdout(buffer):
                code = main_module.main(["--show-config"])

        self.assertEqual(code, 0)
        self.assertIn(config.LAYER_RECALL, buffer.getvalue())


class TestTopicResolution(unittest.TestCase):
    """主题 id 自动解析：改 TOPIC_QUERY 就该换掉检索方向。"""

    def setUp(self):
        self._saved_topics = config.TOPICS
        self._saved_query = config.TOPIC_QUERY
        self._saved_cache = config.TOPICS_CACHE_FILE
        self._tmp = tempfile.TemporaryDirectory()
        config.TOPICS = {}
        config.TOPICS_CACHE_FILE = os.path.join(self._tmp.name, "topics_cache.json")

    def tearDown(self):
        config.TOPICS = self._saved_topics
        config.TOPIC_QUERY = self._saved_query
        config.TOPICS_CACHE_FILE = self._saved_cache
        self._tmp.cleanup()

    @staticmethod
    def _fake_response(payload):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        return _Resp()

    def test_keeps_api_relevance_order_not_works_count(self):
        """必须保留 OpenAlex 的相关性顺序，不能按 works_count 重排。

        回归背景：曾按 works_count 降序排，导致查询 "solid-state battery" 时
        选中 T12646（Inorganic Fluorides，11 万篇的泛主题）而不是 T10281
        （Advanced Battery Materials and Technologies），只能召回 8 篇无关论文。
        """
        payload = {
            "results": [
                {"id": "https://openalex.org/T10281", "display_name": "Battery Materials", "works_count": 78980},
                {"id": "https://openalex.org/T12646", "display_name": "Inorganic Fluorides", "works_count": 111212},
            ]
        }
        with patch.object(openalex_client.requests, "get", return_value=self._fake_response(payload)):
            topics = openalex_client.resolve_topics("solid-state battery")
        # 第一个（相关度最高）获胜，而不是 works_count 最大的那个
        self.assertEqual(list(topics.items()), [("Battery Materials", "T10281")])

    def test_https_prefix_is_stripped(self):
        payload = {"results": [{"id": "https://openalex.org/T10281", "display_name": "X", "works_count": 1}]}
        with patch.object(openalex_client.requests, "get", return_value=self._fake_response(payload)):
            self.assertEqual(openalex_client.resolve_topics("q"), {"X": "T10281"})

    def test_empty_topics_triggers_resolution(self):
        """config.TOPICS 留空时必须走 TOPIC_QUERY 自动解析，而不是静默无过滤。"""
        config.TOPIC_QUERY = "perovskite solar cell"
        payload = {"results": [{"id": "T5000", "display_name": "Perovskite PV", "works_count": 5}]}
        with patch.object(openalex_client.requests, "get", return_value=self._fake_response(payload)):
            self.assertEqual(openalex_client.active_topics(), {"Perovskite PV": "T5000"})

    def test_manual_topics_take_priority(self):
        config.TOPICS = {"Manual": "T7777"}
        with patch.object(openalex_client.requests, "get") as get:
            self.assertEqual(openalex_client.active_topics(), {"Manual": "T7777"})
        get.assert_not_called()

    def test_failure_returns_empty_without_raising(self):
        import requests

        with patch.object(openalex_client.requests, "get", side_effect=requests.RequestException("boom")):
            self.assertEqual(openalex_client.resolve_topics("anything"), {})
            # 解析失败时不能崩，但要退回无主题过滤（由 build_filter 记 WARNING）
            self.assertEqual(openalex_client.topic_filter_value(), "")

    def test_result_is_cached(self):
        payload = {"results": [{"id": "T1", "display_name": "X", "works_count": 1}]}
        with patch.object(openalex_client.requests, "get", return_value=self._fake_response(payload)) as get:
            openalex_client.resolve_topics("cached-query")
            openalex_client.resolve_topics("cached-query")
        self.assertEqual(get.call_count, 1)


class TestParseWork(unittest.TestCase):
    def test_parses_full_record(self):
        raw = {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1016/J.JOULE.2026.102680",
            "display_name": "Stress-coupled lithium transport",
            "publication_date": "2026-09-01",
            "cited_by_count": 3,
            "type": "article",
            "primary_location": {"source": {"display_name": "Joule"}},
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
        }
        work = parse_work(raw)
        assert work is not None
        self.assertEqual(work["doi"], "10.1016/j.joule.2026.102680")
        self.assertEqual(work["doi_url"], "https://doi.org/10.1016/j.joule.2026.102680")
        self.assertEqual(work["journal"], "Joule")
        self.assertEqual(work["abstract"], "Hello world")

    def test_drops_work_without_doi(self):
        self.assertIsNone(parse_work({"id": "https://openalex.org/W1", "display_name": "no doi"}))

    def test_journal_falls_back_to_issn_table(self):
        raw = {
            "doi": "10.1/x",
            "display_name": "T",
            "primary_location": {"source": {"issn_l": "2058-7546"}},
        }
        work = parse_work(raw)
        assert work is not None
        self.assertEqual(work["journal"], "Nature Energy")

    def test_journal_unknown_when_missing(self):
        work = parse_work({"doi": "10.1/y", "display_name": "T", "primary_location": None})
        assert work is not None
        self.assertEqual(work["journal"], "未知期刊")


class TestAiJsonParsing(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            ai_matcher.parse_ai_json('{"relevance": 88}'),
            {"relevance": 88},
        )

    def test_fenced_json(self):
        self.assertEqual(
            ai_matcher.parse_ai_json('```json\n{"relevance": 88}\n```'),
            {"relevance": 88},
        )

    def test_json_with_prose(self):
        raw = '好的，结果如下：{"relevance": 88, "takeaway": "x"} 以上。'
        parsed = ai_matcher.parse_ai_json(raw)
        self.assertEqual(parsed["relevance"], 88)

    def test_garbage_returns_none(self):
        self.assertIsNone(ai_matcher.parse_ai_json("完全不是 JSON"))
        self.assertIsNone(ai_matcher.parse_ai_json(""))
        self.assertIsNone(ai_matcher.parse_ai_json(None))


class TestNormalizeResult(unittest.TestCase):
    def test_string_score_is_cast_to_int(self):
        # 这是原骨架的 TypeError 来源：模型返回字符串分数
        score, _, _ = ai_matcher.normalize_result({"relevance": "85"})
        self.assertIsInstance(score, int)
        self.assertEqual(score, 85)

    def test_float_score_is_rounded(self):
        self.assertEqual(ai_matcher.normalize_result({"relevance": 84.6})[0], 85)

    def test_out_of_range_clamped(self):
        self.assertEqual(ai_matcher.normalize_result({"relevance": 999})[0], 100)
        self.assertEqual(ai_matcher.normalize_result({"relevance": -5})[0], 0)

    def test_bad_score_falls_back_to_zero(self):
        self.assertEqual(ai_matcher.normalize_result({"relevance": "abc"})[0], 0)
        self.assertEqual(ai_matcher.normalize_result({})[0], 0)

    def test_legacy_score_key_supported(self):
        self.assertEqual(ai_matcher.normalize_result({"score": 70})[0], 70)

    def test_takeaway_and_reason_extracted(self):
        score, takeaway, reason = ai_matcher.normalize_result(
            {"relevance": 90, "takeaway": "提出了…", "reason": "属于核心方向"}
        )
        self.assertEqual((score, takeaway, reason), (90, "提出了…", "属于核心方向"))


class TestEvaluateWorksPartition(unittest.TestCase):
    """``evaluate_works`` 必须把结果分成三堆：入选 / 调用失败 / 低于阈值。

    "低于阈值"必须被返回出来 —— main.py 靠它回写"已读"标记。
    候选近 200 篇而邮件只发 20 篇，若不记录这些已判定过的文献，
    下周会被原封不动重新打分一遍，每周白烧 5 倍 AI 费用。
    """

    @staticmethod
    def _works() -> list[dict]:
        return [
            {"doi": "10.1/high", "pub_date": "2026-09-01"},
            {"doi": "10.1/mid", "pub_date": "2026-09-02"},
            {"doi": "10.1/low", "pub_date": "2026-09-03"},
            {"doi": "10.1/boom", "pub_date": "2026-09-04"},
        ]

    def test_partitions_into_three_buckets(self):
        scores = {"10.1/high": 88, "10.1/mid": 62, "10.1/low": 20}

        def fake_call(work, keywords):
            doi = work["doi"]
            if doi == "10.1/boom":
                raise RuntimeError("API 挂了")
            return scores[doi], f"解读 {doi}", "理由"

        with patch.object(ai_matcher, "call_ai", side_effect=fake_call):
            passed, failed, rejected = ai_matcher.evaluate_works(
                self._works(), keywords=["x"], threshold=60, max_workers=2
            )

        self.assertEqual([w["doi"] for w in passed], ["10.1/high", "10.1/mid"])
        self.assertEqual([w["doi"] for w in failed], ["10.1/boom"])
        self.assertEqual([w["doi"] for w in rejected], ["10.1/low"])
        # 失败项不得混进"低于阈值"，否则会被错误地标记为已读而永不重试
        self.assertNotIn("boom", [w["doi"] for w in rejected])

    def test_passed_sorted_by_score_desc(self):
        def fake_call(work, keywords):
            return (70 if work["doi"] == "10.1/low" else 95), "t", "r"

        works = [
            {"doi": "10.1/high", "pub_date": "2026-09-01"},
            {"doi": "10.1/low", "pub_date": "2026-09-02"},
        ]
        with patch.object(ai_matcher, "call_ai", side_effect=fake_call):
            passed, _, _ = ai_matcher.evaluate_works(works, threshold=60, max_workers=2)
        self.assertEqual([w["doi"] for w in passed], ["10.1/high", "10.1/low"])

    def test_empty_input_returns_three_lists(self):
        self.assertEqual(ai_matcher.evaluate_works([], threshold=60), ([], [], []))


class TestS2CircuitBreaker(unittest.TestCase):
    """Semantic Scholar 连续限流时必须熔断，否则每轮白等一分钟。

    实测公共池一旦开始 429，通常整段时间都在 429：8 篇文献 × 3 次重试
    × 指数退避 ≈ 50 秒，且一篇摘要都拿不到。
    """

    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    @staticmethod
    def _reset() -> None:
        abstract_source._s2_state["disabled"] = False
        abstract_source._s2_state["streak"] = 0
        abstract_source._s2_last_call = 0.0

    def test_stops_calling_after_consecutive_429(self):
        class FakeResp:
            status_code = 429
            headers: dict = {}

            def json(self):
                return {}

        calls: list[str] = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResp()

        limit = abstract_source.S2_CIRCUIT_BREAK_AFTER
        with patch.object(abstract_source.requests, "get", side_effect=fake_get), patch.object(
            abstract_source.time, "sleep", lambda _s: None
        ):
            for i in range(limit + 3):
                abstract_source.from_semantic_scholar(f"10.1/paper{i}")

        # 熔断前每篇最多 S2_MAX_RETRIES 次请求；熔断后一篇都不再发
        self.assertEqual(len(calls), limit * abstract_source.S2_MAX_RETRIES)
        self.assertTrue(abstract_source._s2_state["disabled"])

    def test_success_resets_failure_streak(self):
        class OkResp:
            status_code = 200
            headers: dict = {}

            def json(self):
                return {"abstract": "An abstract."}

        with patch.object(abstract_source.requests, "get", return_value=OkResp()), patch.object(
            abstract_source.time, "sleep", lambda _s: None
        ):
            abstract_source._s2_state["streak"] = abstract_source.S2_CIRCUIT_BREAK_AFTER - 1
            self.assertEqual(abstract_source.from_semantic_scholar("10.1/ok"), "An abstract.")
            self.assertEqual(abstract_source._s2_state["streak"], 0)
            self.assertFalse(abstract_source._s2_state["disabled"])


class TestMailerEscaping(unittest.TestCase):
    def test_html_is_escaped(self):
        works = [
            {
                "doi": "10.1/a&b",
                "doi_url": "https://doi.org/10.1/a&b",
                "title": 'Li<i>FePO</i>4 <script>alert("x")</script>',
                "journal": "Nature & Science",
                "pub_date": "2026-09-01",
                "ai_score": 80,
                "ai_takeaway": "含 <b> 与 & 号",
                "ai_reason": '引号 " 测试',
                "abstract": "has abstract",
            }
        ]
        html_body, plain_body = mailer.build_html(
            works, "2026-09-01", lookback_days=14, first_run=False
        )
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)
        self.assertIn("Nature &amp; Science", html_body)
        self.assertIn("Li&lt;i&gt;FePO&lt;/i&gt;4", html_body)
        self.assertIn("https://doi.org/10.1/a&amp;b", html_body)
        # 纯文本备选里保留原文
        self.assertIn("Li<i>FePO</i>4", plain_body)

    def test_empty_results_renders_heartbeat(self):
        html_body, plain_body = mailer.build_html(
            [], "2026-09-01", lookback_days=14, first_run=False
        )
        self.assertIn("本周没有新的相关文献", html_body)
        self.assertIn("本周没有新的相关文献", plain_body)

    def test_overflow_notice(self):
        works = [
            {
                "doi": f"10.1/{i}",
                "title": f"Paper {i}",
                "journal": "Joule",
                "pub_date": "2026-09-01",
                "ai_score": 90 - i,
                "ai_takeaway": "t",
                "ai_reason": "r",
                "abstract": "a",
            }
            for i in range(25)
        ]
        html_body, _ = mailer.build_html(works, "2026-09-01", lookback_days=14, first_run=False)
        self.assertIn("另有 <b>5</b> 篇", html_body)
        # 只渲染前 20 篇（每篇卡片恰好一个"判断理由："）
        self.assertEqual(html_body.count("判断理由："), 20)

    def test_missing_abstract_tag(self):
        works = [
            {
                "doi": "10.1/z",
                "title": "T",
                "journal": "Joule",
                "pub_date": "2026-09-01",
                "ai_score": 70,
                "ai_takeaway": "",
                "ai_reason": "r",
                "abstract": "",
            }
        ]
        html_body, _ = mailer.build_html(works, "2026-09-01", lookback_days=14, first_run=False)
        self.assertIn("依据：标题（摘要缺失）", html_body)


class TestDedupState(unittest.TestCase):
    """v2 状态文件：按主题分区 + 自动迁移 v1/纯数组格式。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "pushed.json")
        self._orig_file = config.PUSHED_FILE
        self._orig_topics = config.RESEARCH_TOPICS
        config.PUSHED_FILE = self.path
        config.RESEARCH_TOPICS = []  # 单方向模式，分区键 = RESEARCH_FIELD

    def tearDown(self):
        config.PUSHED_FILE = self._orig_file
        config.RESEARCH_TOPICS = self._orig_topics
        self.tmp.cleanup()

    # -- 小工具 ---------------------------------------------------------
    def _write(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    def _read(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    # -- 用例 -----------------------------------------------------------
    def test_first_run_and_roundtrip(self):
        topic = config.active_research_topics()[0].key
        self.assertTrue(dedup.is_first_run(topic))
        self.assertEqual(dedup.load_pushed(topic), set())

        works = [
            {"doi": "10.1/a", "openalex_id": "W1"},
            {"doi": "10.1/B", "openalex_id": "W2"},
        ]
        dedup.mark_pushed(works, run_date="2026-09-15", topic_key=topic)

        self.assertFalse(dedup.is_first_run(topic))
        self.assertEqual(dedup.load_pushed(topic), {"10.1/a", "10.1/b"})

        state = dedup.load_state()
        self.assertEqual(state["last_run"], "2026-09-15")
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["topics"][topic], ["10.1/a", "10.1/b"])

        # 再次去重应全部被过滤掉
        self.assertEqual(dedup.filter_new(works, topic_key=topic), [])

        # dry_run 不应改动文件
        before = self._read()
        dedup.mark_pushed([{"doi": "10.1/new"}], run_date="2026-09-16", dry_run=True, topic_key=topic)
        self.assertEqual(self._read(), before)

    def test_topics_are_independent(self):
        """多主题去重：各记各的，同一篇可以出现在两个主题的邮件里。"""
        shared = {"doi": "10.1/shared"}
        dedup.mark_pushed([shared, {"doi": "10.1/only-a"}], run_date="d", topic_key="A")
        dedup.mark_pushed([shared], run_date="d", topic_key="B")

        self.assertEqual(dedup.load_pushed("A"), {"10.1/shared", "10.1/only-a"})
        self.assertEqual(dedup.load_pushed("B"), {"10.1/shared"})
        # 不指定主题时是并集（"这篇到底推过没有"）
        self.assertEqual(dedup.load_pushed(), {"10.1/shared", "10.1/only-a"})

        # A 已推的不影响 B 的判定
        self.assertEqual(dedup.filter_new([{"doi": "10.1/only-a"}], topic_key="B"), [{"doi": "10.1/only-a"}])
        self.assertEqual(dedup.filter_new([{"doi": "10.1/only-a"}], topic_key="A"), [])

        self.assertEqual(dedup.topic_counts(), {"A": 2, "B": 1})
        self.assertEqual(dedup.orphan_topic_keys(["A"]), ["B"])
        self.assertEqual(dedup.orphan_topic_keys(["A", "B"]), [])

    def test_new_topic_keeps_other_partitions(self):
        self._write({"schema_version": 2, "topics": {"A": ["10.1/a"], "B": []}, "last_run": None})
        dedup.save_state(["10.1/c"], topic_key="B", run_date="2026-09-20")
        state = dedup.load_state()
        self.assertEqual(state["topics"]["A"], ["10.1/a"])
        self.assertEqual(state["topics"]["B"], ["10.1/c"])
        self.assertEqual(state["last_run"], "2026-09-20")

    def test_corrupt_state_falls_back_to_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(dedup.load_state()["topics"], {})
        self.assertTrue(dedup.is_first_run())

    def test_v1_global_list_migrates_without_losing_dois(self):
        self._write({"schema_version": 1, "dois": ["10.1/old", "10.1/older"], "last_run": "2026-09-01"})
        state = dedup.load_state()
        self.assertEqual(state["schema_version"], 2)
        # 旧记录整体归入「当前第一个主题」，一条都不丢
        self.assertEqual(state["topics"][dedup.default_topic_key()], ["10.1/old", "10.1/older"])
        self.assertEqual(state["last_run"], "2026-09-01")
        self.assertEqual(dedup.load_pushed(), {"10.1/old", "10.1/older"})
        self.assertFalse(dedup.is_first_run(dedup.default_topic_key()))

    def test_legacy_array_format(self):
        self._write(["10.1/old"])
        self.assertEqual(dedup.load_pushed(), {"10.1/old"})


class TestJournalRanking(unittest.TestCase):
    """期刊档次加权：最终分 = AI 分 + 档次加成。"""

    def _work(self, journal: str, ai: int, issn: str = "", pub_date: str = "2026-09-01") -> dict:
        return {"doi": f"10.1/{journal}", "journal": journal, "issn": issn, "ai_score": ai, "pub_date": pub_date}

    def test_issn_lookup_beats_display_name(self):
        # OpenAlex 返回的 display_name 与配置写法不同，靠 ISSN 兜住
        work = {"journal": "Angewandte Chemie International Edition", "issn": "1521-3773"}
        self.assertEqual(ranking.journal_tier(work), ("Angew", 3))

    def test_falls_back_to_journal_name(self):
        self.assertEqual(ranking.journal_tier({"journal": "Joule", "issn": "1111-2222"}), ("Joule", 7))

    def test_advanced_functional_materials_is_not_advanced_materials(self):
        afm_issn = config.JOURNALS["Advanced Functional Materials"]
        self.assertEqual(ranking.journal_tier({"journal": "Advanced Functional Materials", "issn": afm_issn}), ("其他", 0))
        self.assertEqual(ranking.journal_tier({"journal": "Advanced Materials", "issn": "1521-4095"}), ("AM", 2))

    def test_unknown_journal_has_no_bonus(self):
        self.assertEqual(ranking.journal_tier({"journal": "Some Other Journal", "issn": ""}), ("其他", 0))

    def test_tier_order_follows_config(self):
        self.assertEqual([tier for tier, _ in ranking.tiers()], list(config.JOURNAL_TIERS))
        bonuses = [bonus for _, bonus in ranking.tiers()]
        self.assertEqual(bonuses, sorted(bonuses, reverse=True))  # 顺序即权重顺序

    def test_weighted_score_can_overtake_higher_ai_score(self):
        works = [
            self._work("Energy & Environmental Science", 75),          # 75 + 0 = 75
            self._work("Nature Energy", 68, issn="2058-7546"),         # 68 + 9 = 77
        ]
        ranked = ranking.rank(works)
        self.assertEqual(ranked[0]["journal"], "Nature Energy")
        self.assertEqual(ranked[0]["final_score"], 77)
        self.assertEqual(ranked[0]["journal_tier"], "大子刊")
        self.assertEqual(ranked[1]["journal_bonus"], 0)
        self.assertEqual(ranked[1]["final_score"], 75)

    def test_final_score_ties_break_on_ai_score(self):
        works = [
            self._work("Nature Energy", 60, issn="2058-7546"),          # 60 + 9 = 69
            self._work("Energy & Environmental Science", 69),           # 69 + 0 = 69
        ]
        ranked = ranking.rank(works)
        self.assertEqual([w["final_score"] for w in ranked], [69, 69])
        self.assertEqual(ranked[0]["journal"], "Energy & Environmental Science")  # 同分看真实相关性

    def test_ranking_is_stable_on_full_tie(self):
        first = self._work("Joule", 70, pub_date="2026-09-01")
        second = self._work("Joule", 70, pub_date="2026-09-09")
        ranked = ranking.rank([first, second])
        self.assertEqual([w["pub_date"] for w in ranked], ["2026-09-09", "2026-09-01"])

    def test_breakdown_text(self):
        work = {"ai_score": 62, "journal_tier": "大子刊", "journal_bonus": 9, "final_score": 71}
        self.assertEqual(ranking.breakdown(work), "AI 62 + 大子刊 9 = 71")
        self.assertEqual(ranking.breakdown({"ai_score": 70}), "AI 70")

    def test_annotate_writes_final_score(self):
        work = {"journal": "Joule", "ai_score": 50}
        ranking.annotate([work])
        self.assertEqual(work["journal_tier"], "Joule")
        self.assertEqual(work["journal_bonus"], 7)
        self.assertEqual(work["final_score"], 57)


class TestResearchTopics(unittest.TestCase):
    """多主题配置：RESEARCH_TOPICS（空 = 回到单方向模式）。"""

    def setUp(self):
        self._orig = config.RESEARCH_TOPICS

    def tearDown(self):
        config.RESEARCH_TOPICS = self._orig

    def test_single_direction_fallback(self):
        config.RESEARCH_TOPICS = []
        topics = config.active_research_topics()
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].name, config.RESEARCH_FIELD)
        self.assertEqual(topics[0].topic_query, config.TOPIC_QUERY)
        self.assertEqual(topics[0].keywords, list(config.USER_KEYWORDS))
        self.assertEqual(topics[0].email_title, config.EMAIL_TITLE)

    def test_two_topics_are_normalized(self):
        config.RESEARCH_TOPICS = [
            {
                "name": "富锂锰正极",
                "topic_query": "lithium-rich manganese oxide cathode",
                "keywords": ["富锂锰", "电压衰减"],
                "description": "关注层状富锂材料的电压衰减机理",
            },
            {
                "name": "无负极钠离子电池",
                "topic_query": "anode-free sodium metal battery",
                "keywords": ["无负极"],
                "title": "无负极钠电周报",
                "key": "sodium",
            },
        ]
        topics = config.active_research_topics()
        self.assertEqual([topic.name for topic in topics], ["富锂锰正极", "无负极钠离子电池"])
        self.assertEqual(topics[0].email_title, "富锂锰正极顶刊周报")  # 未指定 title 时自动派生
        self.assertEqual(topics[1].email_title, "无负极钠电周报")
        self.assertEqual(topics[1].key, "sodium")  # 手工指定分区键
        self.assertEqual(topics[0].key, "富锂锰正极")  # 默认 key = name
        self.assertIn("富锂锰", topics[0].brief())
        self.assertIn("电压衰减机理", topics[0].brief())

    def test_duplicate_names_rejected(self):
        config.RESEARCH_TOPICS = [{"name": "A"}, {"name": "A"}]
        with self.assertRaises(RuntimeError):
            config.active_research_topics()

    def test_missing_name_rejected(self):
        config.RESEARCH_TOPICS = [{"topic_query": "x"}]
        with self.assertRaises(RuntimeError):
            config.active_research_topics()

    def test_non_dict_entry_rejected(self):
        config.RESEARCH_TOPICS = ["just a string"]
        with self.assertRaises(RuntimeError):
            config.active_research_topics()

    def test_single_direction_warnings_are_skipped_when_topics_configured(self):
        config.RESEARCH_TOPICS = [{"name": "A", "keywords": []}]
        # 「RESEARCH_TOPICS 已生效」是说明而非错误，走 INFO
        notices = config.config_notices()
        self.assertTrue(any("RESEARCH_TOPICS" in notice for notice in notices))
        # 那五项已不生效，就不该再对它们报警
        globals_ = config.config_warnings("topic")
        self.assertFalse(any("USER_KEYWORDS 为空" in problem for problem in globals_))
        self.assertEqual(globals_, [])
        # 主题级提醒仍会给出，且带上主题名
        topic_problems = config.topic_warnings("topic", config.active_research_topics()[0])
        self.assertTrue(any("USER_KEYWORDS 为空" in problem for problem in topic_problems))
        self.assertTrue(any("A" in problem for problem in topic_problems))


class TestTopicAwareRetrieval(unittest.TestCase):
    """每个主题各用各的检索词与 ISSN 判定。"""

    def test_pick_issn_prefers_configured_eissn(self):
        source = {"issn_l": "0028-0836", "issn": ["0028-0836", "1476-4687"]}
        self.assertEqual(openalex_client._pick_issn(source), "1476-4687")

    def test_pick_issn_falls_back_when_nothing_configured(self):
        self.assertEqual(openalex_client._pick_issn({"issn": ["1111-2222"], "issn_l": "9999-0000"}), "1111-2222")
        self.assertEqual(openalex_client._pick_issn(None), "")
        self.assertEqual(openalex_client._pick_issn({}), "")

    def test_effective_keywords_precedence(self):
        topic = config.ResearchTopic(name="T", keywords=["主题词"])
        self.assertEqual(openalex_client.effective_keywords(["命令行词"], topic), ["命令行词"])
        self.assertEqual(openalex_client.effective_keywords(None, topic), ["主题词"])
        self.assertEqual(openalex_client.effective_keywords(None), list(config.USER_KEYWORDS))

    def test_both_mode_filter_uses_topic_own_words(self):
        from datetime import date

        topic = config.ResearchTopic(name="富锂锰正极", topic_query="lithium-rich cathode", keywords=["富锂锰"])
        with patch.object(openalex_client, "resolve_topics", return_value={"Layered oxides": "T9"}):
            built = build_filter(date(2026, 8, 16), None, mode="both", topic=topic)
        self.assertIn("topics.id:T9", built)
        self.assertIn("富锂锰", built)
        self.assertIn("title_and_abstract.search", built)

    def test_topic_mode_filter_ignores_keywords(self):
        from datetime import date

        topic = config.ResearchTopic(name="A", topic_query="q", keywords=["不该出现"])
        with patch.object(openalex_client, "resolve_topics", return_value={"T": "T1"}):
            built = build_filter(date(2026, 8, 16), None, mode="topic", topic=topic)
        self.assertIn("topics.id:T1", built)
        self.assertNotIn("不该出现", built)


class TestMultiTopicOrchestration(unittest.TestCase):
    """多主题编排：每个主题一封邮件、各记各的已推送记录。"""

    WORKS = [
        {
            "doi": "10.1/shared",
            "openalex_id": "W1",
            "title": "Shared paper",
            "journal": "Joule",
            "issn": "2542-4351",
            "pub_date": "2026-09-01",
            "abstract": "abstract text",
            "doi_url": "https://doi.org/10.1/shared",
        }
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_file = config.PUSHED_FILE
        self._orig_topics = config.RESEARCH_TOPICS
        config.PUSHED_FILE = os.path.join(self.tmp.name, "pushed.json")
        config.RESEARCH_TOPICS = [
            {"name": "富锂锰正极", "topic_query": "lithium rich cathode", "keywords": ["富锂锰"]},
            {"name": "无负极钠离子电池", "topic_query": "anode free sodium", "keywords": ["无负极"]},
        ]
        self.sent: list[tuple[str, str]] = []
        self.fetched: list[str] = []

    def tearDown(self):
        config.PUSHED_FILE = self._orig_file
        config.RESEARCH_TOPICS = self._orig_topics
        self.tmp.cleanup()

    # -- 桩 -------------------------------------------------------------
    def _fake_fetch(self, keywords, lookback_days, max_works=None, mode=None, topic=None, **_kw):
        self.fetched.append(topic.name)
        return [dict(work) for work in self.WORKS]

    @staticmethod
    def _fake_eval(works, keywords=None, threshold=None, topic=None, **_kw):
        passed = [
            dict(work, ai_score=70, ai_takeaway="解读", ai_reason="理由", ai_error=False)
            for work in works
        ]
        return passed, [], []

    def _patches(self):
        return [
            patch.object(main_module, "validate_env", lambda **_kw: None),
            patch.object(main_module.openalex_client, "fetch_works", side_effect=self._fake_fetch),
            patch.object(main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works),
            patch.object(main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval),
            patch.object(
                main_module.mailer,
                "send_mail",
                side_effect=lambda subject, html_body, plain_body, recipients=None: self.sent.append(
                    (subject, html_body)
                ),
            ),
        ]

    def _run(self, argv: list[str] | None = None):
        args = main_module.build_parser().parse_args(argv or [])
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            return main_module.run(args)
        finally:
            for item in patches:
                item.stop()

    # -- 用例 -----------------------------------------------------------
    def test_two_topics_send_two_emails_with_independent_state(self):
        self.assertEqual(self._run(), 0)

        self.assertEqual(self.fetched, ["富锂锰正极", "无负极钠离子电池"])
        self.assertEqual(len(self.sent), 2)

        subjects = [subject for subject, _ in self.sent]
        self.assertTrue(subjects[0].startswith("富锂锰正极顶刊周报"), subjects[0])
        self.assertTrue(subjects[1].startswith("无负极钠离子电池顶刊周报"), subjects[1])
        self.assertNotEqual(subjects[0], subjects[1])
        # 同一篇论文可以同时出现在两个主题的邮件里
        self.assertTrue(all("10.1/shared" in html for _, html in self.sent))

        counts = dedup.topic_counts()
        self.assertEqual(counts, {"富锂锰正极": 1, "无负极钠离子电池": 1})
        self.assertEqual(dedup.load_pushed("富锂锰正极"), {"10.1/shared"})
        self.assertEqual(dedup.load_pushed("无负极钠离子电池"), {"10.1/shared"})

    def test_second_run_is_deduped_per_topic(self):
        self._run()
        self.sent.clear()
        self.fetched.clear()

        self.assertEqual(self._run(), 0)

        # 两个主题都已经记过这篇 → 都发心跳邮件
        self.assertEqual(len(self.sent), 2)
        for subject, _ in self.sent:
            self.assertTrue(subject.endswith("本周无新文献"), subject)
        self.assertEqual(dedup.topic_counts(), {"富锂锰正极": 1, "无负极钠离子电池": 1})

    def test_topic_flag_runs_only_selected_topic(self):
        self.assertEqual(self._run(["--topic", "无负极钠离子电池"]), 0)
        self.assertEqual(self.fetched, ["无负极钠离子电池"])
        self.assertEqual(list(dedup.topic_counts()), ["无负极钠离子电池"])

    def test_topic_flag_accepts_custom_key(self):
        config.RESEARCH_TOPICS = [dict(topic, key="sodium") if topic["name"] == "无负极钠离子电池" else topic
                                 for topic in config.RESEARCH_TOPICS]
        self.assertEqual(self._run(["--topic", "sodium"]), 0)
        self.assertEqual(self.fetched, ["无负极钠离子电池"])

    def test_unknown_topic_flag_fails_fast(self):
        with self.assertRaises(RuntimeError):
            self._run(["--topic", "不存在的主题"])

    def test_one_topic_failure_does_not_block_the_other(self):
        def flaky_fetch(keywords, lookback_days, max_works=None, mode=None, topic=None, **_kw):
            self.fetched.append(topic.name)
            if topic.name == "富锂锰正极":
                raise RuntimeError("检索挂了")
            return [dict(work) for work in self.WORKS]

        args = main_module.build_parser().parse_args([])
        with patch.object(main_module, "validate_env", lambda **_kw: None), patch.object(
            main_module.openalex_client, "fetch_works", side_effect=flaky_fetch
        ), patch.object(
            main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works
        ), patch.object(
            main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval
        ), patch.object(
            main_module.mailer,
            "send_mail",
            side_effect=lambda subject, html_body, plain_body, recipients=None: self.sent.append(
                (subject, html_body)
            ),
        ):
            with self.assertRaises(RuntimeError):
                main_module.run(args)

        self.assertEqual(self.fetched, ["富锂锰正极", "无负极钠离子电池"])
        self.assertEqual(len(self.sent), 1)  # 活下来的主题照常发信

    def test_dry_run_writes_one_preview_per_topic_and_no_state(self):
        outbox = os.path.join(self.tmp.name, "outbox")
        args = main_module.build_parser().parse_args(["--dry-run"])
        with patch.object(main_module, "validate_env", lambda **_kw: None), patch.object(
            main_module, "OUTBOX_DIR", outbox
        ), patch.object(
            main_module.openalex_client, "fetch_works", side_effect=self._fake_fetch
        ), patch.object(
            main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works
        ), patch.object(
            main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval
        ), patch.object(
            main_module.mailer, "send_mail", side_effect=AssertionError("dry-run 不该发邮件")
        ):
            self.assertEqual(main_module.run(args), 0)

        previews = sorted(os.listdir(outbox))
        self.assertEqual(len(previews), 2)
        self.assertTrue(any("富锂锰正极" in name for name in previews), previews)
        self.assertTrue(any("无负极钠离子电池" in name for name in previews), previews)
        # --dry-run 完全无副作用
        self.assertFalse(os.path.exists(config.PUSHED_FILE))

    def test_email_shows_weighted_score_and_bonus(self):
        self._run(["--topic", "富锂锰正极"])
        _, html_body = self.sent[0]
        self.assertIn("最终 77 分", html_body)  # 70（AI）+ 7（Joule）
        self.assertIn("Joule +7（AI 70）", html_body)

    def test_rule_excluded_work_never_reaches_the_email(self):
        """被内容规则剔除的文献：不进 AI、不进邮件、不进去重库。"""

        def fetch(keywords, lookback_days, max_works=None, mode=None, topic=None, **_kw):
            self.fetched.append(topic.name)
            return [
                dict(self.WORKS[0]),
                {
                    "doi": "10.1/sep",
                    "openalex_id": "W2",
                    "title": "Separator modification for lithium-sulfur batteries",
                    "journal": "Joule",
                    "issn": "2542-4351",
                    "pub_date": "2026-09-02",
                    "abstract": "abstract text",
                    "doi_url": "https://doi.org/10.1/sep",
                },
            ]

        args = main_module.build_parser().parse_args(["--topic", "富锂锰正极"])
        with patch.object(main_module, "validate_env", lambda **_kw: None), patch.object(
            main_module.openalex_client, "fetch_works", side_effect=fetch
        ), patch.object(
            main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works
        ), patch.object(
            main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval
        ), patch.object(
            main_module.mailer,
            "send_mail",
            side_effect=lambda subject, html_body, plain_body, recipients=None: self.sent.append(
                (subject, html_body)
            ),
        ):
            self.assertEqual(main_module.run(args), 0)

        _, html_body = self.sent[0]
        self.assertIn("10.1/shared", html_body)
        self.assertNotIn("10.1/sep", html_body)
        self.assertIn("规则剔除 1 篇", html_body)
        self.assertEqual(dedup.load_pushed("富锂锰正极"), {"10.1/shared"})

    def test_show_config_lists_content_rules(self):
        from src import main as main_module

        args = main_module.build_parser().parse_args(["--show-config"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main_module.show_config(args)

        text = buffer.getvalue()
        # 「靠什么排序 / 靠什么剔除」也要能离线看到，不然改规则全靠猜
        self.assertIn("内容加分（全局）", text)
        self.assertIn("固态电池 +1", text)
        self.assertIn("无负极 +3", text)
        self.assertIn("剔除规则（全局）", text)
        self.assertIn("电解液工程", text)
        self.assertIn("隔膜改性", text)

    def test_show_config_lists_every_topic(self):
        from src import main as main_module

        args = main_module.build_parser().parse_args(["--show-config"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main_module.show_config(args)

        self.assertEqual(code, 0)
        text = buffer.getvalue()
        # 两个主题各自回显自己的检索短语，否则看了也不知道在搜什么
        self.assertIn("lithium rich cathode", text)
        self.assertIn("anode free sodium", text)
        self.assertIn("2 个（来源：RESEARCH_TOPICS", text)
        # 多主题下那五项常数已不生效：只给 INFO 说明，不再对它们报警
        self.assertIn("已配置 RESEARCH_TOPICS", text)
        self.assertNotIn("USER_KEYWORDS 为空", text)


class TestContentBonusRules(unittest.TestCase):
    """内容加分：固态电池 +1、固态聚合物电解质再 +1、无负极 +3。

    规则本身写在 ``config.BONUS_RULES`` 里，这里锁的是**行为**：
    归一化怎么写、能不能叠加、凝胶电解质会不会被误判。
    """

    def test_normalize_flattens_case_and_punctuation(self):
        self.assertEqual(
            content_rules.normalize("All-Solid-State—Battery!"), "all solid state battery"
        )
        self.assertEqual(content_rules.normalize("P2-type  cathode"), "p2 type cathode")
        self.assertEqual(content_rules.normalize(None), "")

    def test_solid_state_paper_gets_one_point(self):
        work = {"title": "All-solid-state lithium batteries with a sulfide electrolyte"}
        self.assertEqual(content_rules.bonus_score(work), 1)
        self.assertEqual([label for label, _ in content_rules.matched_bonuses(work)], ["固态电池"])

    def test_solid_polymer_electrolyte_stacks_on_solid_state(self):
        work = {"title": "Solid polymer electrolyte for solid-state lithium batteries"}
        labels = [label for label, _ in content_rules.matched_bonuses(work)]
        self.assertEqual(labels, ["固态电池", "固态聚合物电解质"])
        self.assertEqual(content_rules.bonus_score(work), 2)

    def test_gel_electrolyte_is_not_a_solid_polymer_electrolyte(self):
        work = {"title": "Gel polymer electrolyte enables a high-voltage cathode"}
        self.assertEqual(content_rules.bonus_score(work), 0)

    def test_anode_free_gets_three_points(self):
        work = {"title": "Anode-free sodium metal batteries"}
        self.assertEqual(content_rules.bonus_score(work), 3)

    def test_bonus_can_be_triggered_by_the_abstract(self):
        """加分是"排序依据"，宁滥勿缺：摘要里提到也算。"""
        work = {"title": "A new cathode design", "abstract": "assembled in an anode-free cell"}
        self.assertEqual(content_rules.bonus_score(work), 3)

    def test_unrelated_paper_gets_nothing(self):
        work = {"title": "Anionic redox in Li-rich layered oxides", "abstract": "voltage decay"}
        self.assertEqual(content_rules.bonus_score(work), 0)

    def test_layered_sodium_cathode_earns_the_topic_bonus(self):
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        work = {"title": "Air-stable O3-type layered oxide cathode for sodium-ion batteries"}
        self.assertEqual(content_rules.bonus_score(work, sodium), 2)

    def test_layered_lithium_cathode_does_not_earn_the_sodium_bonus(self):
        """层状 ≠ 钠电：必须同时是钠离子体系（规则的 all 字段）。"""
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        work = {"title": "P2-type layered oxide cathode for lithium-ion batteries"}
        self.assertEqual(content_rules.bonus_score(work, sodium), 0)

    def test_topic_bonus_is_invisible_to_other_topics(self):
        lithium = next(t for t in config.active_research_topics() if t.name == "富锂锰正极")
        work = {"title": "Layered oxide cathode for sodium-ion batteries"}
        self.assertEqual(content_rules.bonus_score(work, lithium), 0)

    def test_rule_matching_supports_unless_and_all(self):
        rule = {"any": ["layered"], "all": ["sodium"], "unless": ["prussian blue"]}
        self.assertTrue(content_rules.rule_matches(rule, content_rules.normalize("sodium layered oxide")))
        self.assertFalse(content_rules.rule_matches(rule, content_rules.normalize("lithium layered oxide")))
        self.assertFalse(
            content_rules.rule_matches(rule, content_rules.normalize("sodium layered prussian blue"))
        )


class TestContentExclusions(unittest.TestCase):
    """剔除规则：不看电解液工程、不看隔膜改性。"""

    def test_electrolyte_additive_paper_is_dropped(self):
        work = {"title": "Electrolyte additives for high-voltage lithium batteries"}
        self.assertEqual(content_rules.exclusion_hit(work), "电解液工程")

    def test_separator_paper_is_dropped(self):
        work = {"title": "Functional separator design for lithium-sulfur batteries"}
        self.assertEqual(content_rules.exclusion_hit(work), "隔膜改性")

    def test_solid_state_work_is_not_mistaken_for_electrolyte_engineering(self):
        """固态体系不该被"电解液"三个字误伤（规则里的 unless 兜底）。"""
        work = {"title": "Electrolyte additives for all-solid-state batteries"}
        self.assertIsNone(content_rules.exclusion_hit(work))

    def test_exclusion_looks_at_the_title_only(self):
        """按摘要剔除会误杀 —— 相关论文也常把电解液添加剂当对比组。"""
        work = {"title": "A high-capacity Li-rich cathode", "abstract": "compared to electrolyte additives"}
        self.assertIsNone(content_rules.exclusion_hit(work))

    def test_relevant_cathode_paper_passes(self):
        work = {"title": "Anionic redox in Li-rich layered oxides"}
        self.assertIsNone(content_rules.exclusion_hit(work))

    def test_partition_marks_the_reason(self):
        works = [{"title": "Separator modification for Li-S"}, {"title": "keep me"}]
        kept, dropped = content_rules.partition_excluded(works)
        self.assertEqual([w["title"] for w in kept], ["keep me"])
        self.assertEqual(dropped[0]["exclude_reason"], "隔膜改性")

    def test_topic_specific_exclusion_is_scoped(self):
        """主题自己的 exclude 只对该主题生效。"""
        scoped = config.ResearchTopic(
            name="A", keywords=["x"], exclude=[{"label": "本主题不看", "any": ["prussian blue"]}]
        )
        work = {"title": "Prussian blue analogue cathode"}
        self.assertEqual(content_rules.exclusion_hit(work, scoped), "本主题不看")
        self.assertIsNone(content_rules.exclusion_hit(work))


class TestContentRuleValidation(unittest.TestCase):
    """规则写错只会静默失效，所以必须有地方报出来。"""

    def test_live_config_rules_are_healthy(self):
        self.assertEqual(config.validate_content_rules(), [])

    def test_empty_any_is_reported(self):
        with patch.object(config, "BONUS_RULES", [{"label": "坏的", "score": 1, "any": []}]):
            problems = config.validate_content_rules()
        self.assertTrue(any("永远不会生效" in p for p in problems), problems)

    def test_missing_label_and_bad_score_are_reported(self):
        with patch.object(config, "EXCLUDE_RULES", [{"score": "多", "any": ["x"]}]):
            problems = config.validate_content_rules()
        self.assertTrue(any('"label"' in p for p in problems), problems)
        self.assertTrue(any('"score"' in p for p in problems), problems)

    def test_rule_problems_surface_as_config_warnings(self):
        with patch.object(config, "BONUS_RULES", [{"label": "坏的", "score": 1}]):
            problems = config.config_warnings("topic")
        self.assertTrue(any("永远不会生效" in p for p in problems), problems)


class TestContentBonusAffectsRanking(unittest.TestCase):
    """最终分 = AI 分 + 期刊档次加成 + 内容规则加成。"""

    def test_final_score_adds_up_all_three_layers(self):
        work = {
            "ai_score": 70,
            "issn": "2058-7546",  # Nature Energy → 大子刊 +9
            "title": "Anode-free solid-state lithium batteries",
        }
        ranking.annotate([work])
        self.assertEqual(work["journal_bonus"], 9)
        self.assertEqual(work["content_bonus"], 4)  # 固态 1 + 无负极 3
        self.assertEqual(work["final_score"], 83)
        self.assertEqual(work["ai_score"], 70)  # AI 分本身不动

    def test_content_bonus_does_not_lower_the_threshold(self):
        """加分只改排序：低分论文不会被"抬"过入选线（入选在 AI 打分那一步就定了）。"""
        work = {"ai_score": 10, "title": "Anode-free solid-state battery", "issn": "1476-4687"}
        ranking.annotate([work])
        self.assertEqual(work["ai_score"], 10)
        self.assertLess(work["ai_score"], config.AI_THRESHOLD)

    def test_breakdown_lists_content_items(self):
        work = {
            "ai_score": 70,
            "journal_tier": "其他",
            "journal_bonus": 0,
            "content_bonus": 4,
            "content_bonus_detail": [["固态电池", 1], ["无负极", 3]],
            "final_score": 74,
        }
        self.assertEqual(ranking.breakdown(work), "AI 70 + 固态电池 1 + 无负极 3 = 74")

    def test_content_bonus_can_overtake_a_higher_ai_score(self):
        plain = {"doi": "a", "ai_score": 72, "issn": "2542-4351", "title": "plain cathode work"}
        boosted = {"doi": "b", "ai_score": 70, "issn": "2542-4351", "title": "Anode-free battery"}
        self.assertEqual([w["doi"] for w in ranking.rank([plain, boosted])], ["b", "a"])

    def test_rank_accepts_a_topic(self):
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        work = {
            "doi": "x",
            "ai_score": 60,
            "issn": "2041-1723",  # Nature Communications → 小子刊 +5
            "title": "O3-type layered oxide cathode for sodium-ion batteries",
        }
        ranking.rank([work], sodium)
        self.assertEqual(work["content_bonus"], 2)
        self.assertEqual(work["final_score"], 67)


class TestMailerContentTags(unittest.TestCase):
    """邮件卡片要把内容加分显示出来，否则"为什么它排前面"看不出来。"""

    def test_card_shows_content_tags_and_final_score(self):
        work = {
            "doi": "10.1/x",
            "title": "Anode-free solid-state battery with a polymer electrolyte",
            "journal": "Joule",
            "issn": "2542-4351",
            "pub_date": "2026-09-01",
            "abstract": "abstract text",
            "ai_score": 70,
            "ai_takeaway": "解读",
            "ai_reason": "理由",
        }
        html_body, plain = mailer.build_html(
            ranking.rank([work]), "2026-09-01", lookback_days=14, first_run=False, excluded=3
        )
        # 70（AI）+ 7（Joule）+ 1（固态）+ 3（无负极）+ 1（固态聚合物电解质）
        self.assertIn("最终 82 分", html_body)
        self.assertIn("Joule +7（AI 70）", html_body)
        self.assertIn("固态电池 +1", html_body)
        self.assertIn("无负极 +3", html_body)
        self.assertIn("规则剔除 3 篇", html_body)
        self.assertIn("固态电池 1", plain)


class TestLiveResearchConfig(unittest.TestCase):
    """把"用户当前想要的课题"钉进测试，防止后续改动悄悄回退。"""

    def test_live_topics_are_lithium_rich_and_sodium_cathode(self):
        self.assertEqual(
            [topic.name for topic in config.active_research_topics()], ["富锂锰正极", "钠离子正极"]
        )

    def test_bonus_scores_match_the_request(self):
        scores = {rule["label"]: rule["score"] for rule in config.BONUS_RULES}
        self.assertEqual(scores["固态电池"], 1)
        self.assertEqual(scores["固态聚合物电解质"], 1)  # 在"固态"之上再加 1
        self.assertEqual(scores["无负极"], 3)

    def test_exclusions_are_electrolyte_engineering_and_separator(self):
        self.assertEqual(
            [rule["label"] for rule in config.EXCLUDE_RULES], ["电解液工程", "隔膜改性"]
        )

    def test_sodium_topic_carries_the_layered_bonus(self):
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        self.assertIn("层状钠离子正极", [rule["label"] for rule in sodium.bonuses])

    def test_every_topic_has_keywords_and_a_description(self):
        for topic in config.active_research_topics():
            self.assertTrue(topic.keywords, topic.name)
            self.assertTrue(topic.description.strip(), topic.name)

    def test_global_exclude_note_is_fed_to_the_ai(self):
        self.assertTrue(config.GLOBAL_EXCLUDE_NOTE.strip())
        for topic in config.active_research_topics():
            self.assertIn(config.GLOBAL_EXCLUDE_NOTE.split("；")[0], topic.brief())


if __name__ == "__main__":
    unittest.main(verbosity=2)
