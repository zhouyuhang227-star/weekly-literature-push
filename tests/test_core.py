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

from src import abstract_source, ai_matcher, config, mailer  # noqa: E402
from src import openalex_client  # noqa: E402
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

    def test_default_mode_is_topic(self):
        """默认参数就应当是 topic —— 防止有人悄悄改回去。"""
        from datetime import date

        self.assertEqual(config.RETRIEVAL_MODE, "topic")
        self.assertIn("topics.id:", build_filter(date(2026, 8, 16), ["x"]))


class TestResearchDirectionIsConfigurable(unittest.TestCase):
    """换研究方向必须只改 config，且不能留下静默用错方向的空间。

    这些测试存在的意义：一旦有人在源码里重新写死"固态电池/电池材料"，
    或者让主题 id 与关键词脱钩（改了关键词却仍检索旧主题），这里会立刻变红。
    """

    def setUp(self):
        self._saved = {
            name: getattr(config, name)
            for name in ("RESEARCH_FIELD", "RESEARCH_DESCRIPTION", "USER_KEYWORDS")
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
        """评分档位里的"固态电解质、锂金属负极"这类示例必须已经清除。"""
        config.USER_KEYWORDS = ["perovskite solar cell"]
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        for stale in ("固态电解质", "锂金属负极", "液态电解液", "钠离子电池"):
            self.assertNotIn(stale, prompt)

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
    """只报真正配错的组合，不狼来了。"""

    def setUp(self):
        self._saved = {
            name: getattr(config, name) for name in ("USER_KEYWORDS", "TOPIC_QUERY", "TOPICS")
        }

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

    def test_topic_mode_without_any_topic_source_warns(self):
        config.TOPICS = {}
        config.TOPIC_QUERY = "   "
        problems = config.config_warnings("topic")
        self.assertTrue(any("退化成无主题过滤" in p for p in problems))

        # 手工锁定了 TOPICS 后就不该再报
        config.TOPICS = {"X": "T1"}
        self.assertEqual(config.config_warnings("topic"), [])

    def test_bad_mode_warns(self):
        self.assertTrue(any("不是合法值" in p for p in config.config_warnings("typo")))


class TestShowConfigCommand(unittest.TestCase):
    """--show-config 必须离线、不跑主流程，并把两层分工讲清楚。"""

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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "pushed.json")
        self._orig = None

    def tearDown(self):
        self.tmp.cleanup()

    def _patch(self):
        from src import config

        self._orig = config.PUSHED_FILE
        config.PUSHED_FILE = self.path
        import src.dedup as dedup

        self._orig_dedup = dedup.PUSHED_FILE
        dedup.PUSHED_FILE = self.path

    def _unpatch(self):
        from src import config
        import src.dedup as dedup

        config.PUSHED_FILE = self._orig
        dedup.PUSHED_FILE = self._orig_dedup

    def test_first_run_and_roundtrip(self):
        self._patch()
        try:
            import src.dedup as dedup

            self.assertTrue(dedup.is_first_run())
            self.assertEqual(dedup.load_pushed(), set())

            works = [
                {"doi": "10.1/a", "openalex_id": "W1"},
                {"doi": "10.1/B", "openalex_id": "W2"},
            ]
            dedup.mark_pushed(works, run_date="2026-09-15")

            self.assertFalse(dedup.is_first_run())
            self.assertEqual(dedup.load_pushed(), {"10.1/a", "10.1/b"})

            state = dedup.load_state()
            self.assertEqual(state["last_run"], "2026-09-15")
            self.assertEqual(state["schema_version"], 1)

            # 再次去重应全部被过滤掉
            self.assertEqual(dedup.filter_new(works), [])

            # dry_run 不应改动文件
            with open(self.path, encoding="utf-8") as handle:
                before = json.load(handle)
            dedup.mark_pushed([{"doi": "10.1/new"}], run_date="2026-09-16", dry_run=True)
            with open(self.path, encoding="utf-8") as handle:
                after = json.load(handle)
            self.assertEqual(before, after)
        finally:
            self._unpatch()

    def test_corrupt_state_falls_back_to_empty(self):
        self._patch()
        try:
            import src.dedup as dedup

            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            self.assertEqual(dedup.load_state()["dois"], [])
            self.assertTrue(dedup.is_first_run())
        finally:
            self._unpatch()

    def test_legacy_array_format(self):
        self._patch()
        try:
            import src.dedup as dedup

            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(["10.1/old"], handle)
            self.assertEqual(dedup.load_pushed(), {"10.1/old"})
        finally:
            self._unpatch()


if __name__ == "__main__":
    unittest.main(verbosity=2)
