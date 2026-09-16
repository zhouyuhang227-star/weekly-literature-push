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
from src import sources as source_layer  # noqa: E402
from src.sources import base as source_base  # noqa: E402
from src.sources import crossref as crossref_source  # noqa: E402
from src.sources import semantic_scholar as semantic_scholar_source  # noqa: E402
from src.openalex_client import (  # noqa: E402
    build_filter,
    build_keyword_query,
    normalize_doi,
    parse_work,
    reconstruct_abstract,
)


def _pdf_text(payload: bytes) -> str:
    """把附件 PDF 里的文字抠出来，供断言用（不引入第三方 PDF 库）。

    手写 PDF 的正文流是 FlateDecode 压缩的、每行文字是 UTF-16BE 的 hex 串，
    这里手工解一遍。只在单测里用，生产代码不需要反向解析。
    """
    import re as _re
    import zlib as _zlib

    document = payload.decode("latin-1")
    chunks: list[str] = []
    for stream in _re.findall(r"stream\r?\n(.*?)\r?\nendstream", document, _re.S):
        ops = _zlib.decompress(stream.encode("latin-1")).decode("latin-1")
        chunks += [
            bytes.fromhex(hex_text).decode("utf-16-be")
            for hex_text in _re.findall(r"<([0-9A-Fa-f]+)>\s*Tj", ops)
        ]
    return "\n".join(chunks)


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


class TestOpenAlexQuotaErrors(unittest.TestCase):
    """OpenAlex 2026 起按积分计费：额度耗尽的 429 必须与突发限流区分开。

    区别很实际：突发限流退避几秒就好；额度耗尽要等次日 UTC 零点，
    重试纯属白等，而且日志里只留一句「HTTP 429」时会让人以为是网络抽风。
    """

    class _Resp:
        def __init__(self, status_code, body=None, headers=None):
            self.status_code = status_code
            self._body = {} if body is None else body
            self.headers = headers or {}
            self.text = json.dumps(self._body, ensure_ascii=False)

        def json(self):
            return self._body

    def test_budget_exhaustion_is_recognised_from_the_message(self):
        resp = self._Resp(429, {"error": "Rate limit exceeded", "message": "Insufficient budget."})
        self.assertTrue(openalex_client._is_budget_exhausted(resp))

    def test_burst_429_is_not_mistaken_for_budget_exhaustion(self):
        resp = self._Resp(429, {"error": "Too many requests"}, {"Retry-After": "3"})
        self.assertFalse(openalex_client._is_budget_exhausted(resp))

    def test_budget_message_says_why_and_how_to_fix(self):
        resp = self._Resp(
            429,
            {"message": "Insufficient budget."},
            {"Retry-After": "77000", "X-RateLimit-Limit": "1000"},
        )
        text = openalex_client._budget_message(resp)
        self.assertIn("今日额度已用完", text)
        self.assertIn("小时", text)
        self.assertIn("OPENALEX_API_KEY", text)

    def test_request_page_gives_up_immediately_when_quota_is_gone(self):
        calls: list[int] = []
        resp = self._Resp(
            429, {"message": "Insufficient budget."}, {"X-RateLimit-Remaining": "0"}
        )
        with patch.object(
            openalex_client.requests, "get", side_effect=lambda *a, **k: calls.append(1) or resp
        ), patch.object(openalex_client.time, "sleep", lambda _s: None):
            with self.assertRaises(RuntimeError) as ctx:
                openalex_client._request_page({"filter": "x"}, attempt=3)
        self.assertEqual(len(calls), 1)  # 不做无意义的重试
        self.assertIn("额度", str(ctx.exception))

    def test_request_page_does_not_retry_malformed_queries(self):
        calls: list[int] = []
        resp = self._Resp(400, {"error": "Bad filter"})
        with patch.object(
            openalex_client.requests, "get", side_effect=lambda *a, **k: calls.append(1) or resp
        ), patch.object(openalex_client.time, "sleep", lambda _s: None):
            with self.assertRaises(RuntimeError) as ctx:
                openalex_client._request_page({"filter": "bad"}, attempt=3)
        self.assertEqual(len(calls), 1)
        self.assertIn("filter=bad", str(ctx.exception))

    def test_request_page_still_retries_a_transient_429(self):
        ok = self._Resp(200, {"meta": {"count": 1}, "results": []})
        queue = [self._Resp(429, {"error": "Too many requests"}, {"Retry-After": "2"}), ok]
        calls: list[int] = []

        def fake_get(*_args, **_kwargs):
            calls.append(1)
            return queue.pop(0)

        with patch.object(openalex_client.requests, "get", side_effect=fake_get), patch.object(
            openalex_client.time, "sleep", lambda _s: None
        ):
            payload = openalex_client._request_page({"filter": "x"}, attempt=3)
        self.assertEqual(payload["meta"]["count"], 1)
        self.assertEqual(len(calls), 2)

    def test_api_key_is_attached_only_when_configured(self):
        saved = config.OPENALEX_API_KEY
        try:
            config.OPENALEX_API_KEY = ""
            self.assertEqual(openalex_client._with_auth({"filter": "x"}), {"filter": "x"})
            config.OPENALEX_API_KEY = "k-123"
            self.assertEqual(openalex_client._with_auth({"filter": "x"})["api_key"], "k-123")
        finally:
            config.OPENALEX_API_KEY = saved


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

        注意：这里也要把 ``GLOBAL_EXCLUDE_NOTE`` / ``AI_ANODE_FREE_HINT`` /
        ``AI_CATHODE_HINT`` 清空 —— 那些都是用户能在 config 里改的**配置文本**
        （会拼进 prompt），不是模板里写死的学科词。
        """
        config.USER_KEYWORDS = ["perovskite solar cell"]
        config.GLOBAL_EXCLUDE_NOTE = ""
        config.AI_ANODE_FREE_HINT = ""
        config.AI_CATHODE_HINT = ""
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        for stale in ("固态电解质", "锂金属负极", "液态电解液", "钠离子电池"):
            self.assertNotIn(stale, prompt)

    def test_user_prompt_asks_for_the_anode_free_judgement(self):
        """★ AI 要能回答两个结构化问题，否则第二层保底无从谈起。"""
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        self.assertIn('"anode_free"', prompt)
        self.assertIn('"cathode_system"', prompt)
        self.assertIn("li-rich-mn", prompt)
        self.assertIn("sodium", prompt)
        # 判定为无负极时 takeaway 必须带这三个字，否则用户看不出它为什么被置顶
        self.assertIn("无负极", prompt)

    def test_user_prompt_carries_the_user_configurable_hints(self):
        config.AI_ANODE_FREE_HINT = "自定义无负极口径"
        config.AI_CATHODE_HINT = "自定义体系口径"
        prompt = ai_matcher.build_prompt({"title": "T"}, None)
        self.assertIn("自定义无负极口径", prompt)
        self.assertIn("自定义体系口径", prompt)

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
        self.assertIn("search_terms", hint)
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
            return scores[doi], f"解读 {doi}", "理由", False, ""

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
            return (70 if work["doi"] == "10.1/low" else 95), "t", "r", False, ""

        works = [
            {"doi": "10.1/high", "pub_date": "2026-09-01"},
            {"doi": "10.1/low", "pub_date": "2026-09-02"},
        ]
        with patch.object(ai_matcher, "call_ai", side_effect=fake_call):
            passed, _, _ = ai_matcher.evaluate_works(works, threshold=60, max_workers=2)
        self.assertEqual([w["doi"] for w in passed], ["10.1/high", "10.1/low"])

    def test_empty_input_returns_three_lists(self):
        self.assertEqual(ai_matcher.evaluate_works([], threshold=60), ([], [], []))


class TestEvaluateWorksForcedKeep(unittest.TestCase):
    """AI 那一层的硬保底：分数再低也进"入选"，AI 挂了也要进。

    这一层最先出问题的地方是**划分顺序**：如果先按阈值分堆、再挑保底，
    一篇 20 分的无负极论文会因为"低于阈值"被回写已读标记而永远不见天日。
    """

    def _keep_work(self) -> dict:
        return {"doi": "10.1/keep", "title": "Anode-free sodium metal battery", "pub_date": "2026-09-01"}

    def test_low_score_keep_bypasses_the_threshold(self):
        with patch.object(ai_matcher, "call_ai", return_value=(20, "解读", "理由", False, "")):
            passed, failed, rejected = ai_matcher.evaluate_works([self._keep_work()], threshold=60)

        self.assertEqual([w["doi"] for w in passed], ["10.1/keep"])
        self.assertEqual(passed[0]["keep_reason"], "无负极")
        # 不得同时落进"低于阈值"：main.py 会据此回写已读标记
        self.assertEqual(rejected, [])
        self.assertEqual(failed, [])

    def test_keep_survives_an_ai_outage(self):
        """AI 调用失败时也必须发出：宁可少一段解读，不能丢文献。"""
        with patch.object(ai_matcher, "call_ai", side_effect=RuntimeError("API 挂了")):
            passed, failed, rejected = ai_matcher.evaluate_works([self._keep_work()], threshold=60)

        self.assertEqual([w["doi"] for w in passed], ["10.1/keep"])
        self.assertEqual(failed, [])  # 进了"失败"堆就不会出现在邮件里
        self.assertEqual(rejected, [])
        self.assertEqual(passed[0]["ai_score"], 0)
        self.assertIn("保底规则", passed[0]["ai_reason"])

    def test_low_score_non_keep_is_still_rejected(self):
        work = {"doi": "10.1/low", "title": "irrelevant work", "pub_date": "2026-09-01"}
        with patch.object(ai_matcher, "call_ai", return_value=(20, "解读", "理由", False, "")):
            passed, failed, rejected = ai_matcher.evaluate_works([work], threshold=60)
        self.assertEqual(passed, [])
        self.assertEqual(failed, [])
        self.assertEqual([w["doi"] for w in rejected], ["10.1/low"])

    def test_outage_on_a_non_keep_paper_still_goes_to_failed(self):
        work = {"doi": "10.1/boom", "title": "irrelevant work", "pub_date": "2026-09-01"}
        with patch.object(ai_matcher, "call_ai", side_effect=RuntimeError("API 挂了")):
            passed, failed, rejected = ai_matcher.evaluate_works([work], threshold=60)
        self.assertEqual([w["doi"] for w in failed], ["10.1/boom"])
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_real_li_s_paper_no_longer_rides_the_ai_keep_channel(self):
        """★ 端到端复现那起事故（真实论文，DOI 10.1002/anie.2370748）。

        它**确实是**无负极（AI 也这么判），但正极是 Li2S ⇒ `cathode_system="other"`。
        Seg Q 之前，它会被保底无条件拉进"入选"并置顶；现在必须落回"低于阈值"
        （AI 给 35 < 60），而且**不能**被写上 `keep_reason`。
        """
        work = li_s_anode_free_work(pub_date="2026-09-01")
        with patch.object(ai_matcher, "call_ai", return_value=(35, "解读", "理由", True, "other")):
            passed, failed, rejected = ai_matcher.evaluate_works([work], threshold=60)

        self.assertEqual(passed, [])
        self.assertEqual(failed, [])
        self.assertEqual([w["doi"] for w in rejected], ["10.1002/anie.2370748"])
        self.assertNotIn("keep_reason", rejected[0])

    def test_ai_keep_channel_still_works_for_a_matching_system(self):
        """反向对照：同一批 AI 判断，只要体系对口就必须保底 —— 别把整条通道关掉。"""
        work = li_s_anode_free_work(pub_date="2026-09-01")
        with patch.object(ai_matcher, "call_ai", return_value=(35, "解读", "理由", True, "sodium")):
            passed, _, rejected = ai_matcher.evaluate_works([work], threshold=60)

        self.assertEqual([w["doi"] for w in passed], ["10.1002/anie.2370748"])
        self.assertEqual(passed[0]["keep_reason"], "无负极（AI 判定 · 钠电）")
        self.assertEqual(rejected, [])

    def test_keep_paper_keeps_its_real_score_when_the_ai_worked(self):
        """保底只免掉阈值，不改分数 —— 分数照旧参与排序。"""
        with patch.object(ai_matcher, "call_ai", return_value=(58, "解读", "理由", False, "")):
            passed, _, _ = ai_matcher.evaluate_works([self._keep_work()], threshold=60)
        self.assertEqual(passed[0]["ai_score"], 58)
        self.assertFalse(passed[0]["ai_error"])


class TestForcedKeepRanking(unittest.TestCase):
    """保底论文不仅要"留下"，还要排在最前 —— 否则会掉进 PDF 附件里没人看。

    置顶看起来违背"按分数排"，所以卡片上会打一个绿色标签解释原因；
    这里锁的是排序行为本身。
    """

    @staticmethod
    def _works() -> list[dict]:
        return [
            {"doi": "10.1/plain", "ai_score": 95, "pub_date": "2026-09-01", "title": "plain work"},
            {
                "doi": "10.1/keep",
                "ai_score": 55,
                "pub_date": "2026-09-02",
                "title": "Anode-free sodium metal battery",
            },
        ]

    def test_keep_is_pinned_above_a_higher_ai_score(self):
        self.assertEqual([w["doi"] for w in ranking.rank(self._works())], ["10.1/keep", "10.1/plain"])

    def test_annotate_writes_force_keep(self):
        work = {"ai_score": 50, "title": "Anode-free sodium metal battery"}
        ranking.annotate([work])
        self.assertTrue(work["force_keep"])
        self.assertEqual(work["content_bonus"], 10)

    def test_non_kept_work_is_not_pinned(self):
        work = {"ai_score": 50, "title": "Anionic redox in Li-rich layered oxides"}
        ranking.annotate([work])
        self.assertFalse(work["force_keep"])

    def test_order_within_the_same_group_is_still_by_final_score(self):
        works = [
            {
                "doi": "10.1/keep-low",
                "ai_score": 30,
                "pub_date": "2026-09-01",
                "title": "Anode-free sodium battery A",
            },
            {
                "doi": "10.1/keep-high",
                "ai_score": 45,
                "pub_date": "2026-09-02",
                "title": "Anode-free sodium battery B",
            },
            {"doi": "10.1/plain-high", "ai_score": 90, "pub_date": "2026-09-03", "title": "plain A"},
            {"doi": "10.1/plain-low", "ai_score": 70, "pub_date": "2026-09-04", "title": "plain B"},
        ]
        self.assertEqual(
            [w["doi"] for w in ranking.rank(works)],
            ["10.1/keep-high", "10.1/keep-low", "10.1/plain-high", "10.1/plain-low"],
        )

    def test_forced_count(self):
        self.assertEqual(ranking.forced_count([{"force_keep": True}, {}, {"force_keep": False}]), 1)
        self.assertEqual(ranking.forced_count([]), 0)

    def test_describe_mentions_the_pinning(self):
        self.assertIn("保底", ranking.describe())


class TestAiDetectedAnodeFreeKeep(unittest.TestCase):
    """第二层保底：关键词一个都没命中，但 AI 从摘要里读出了「无负极 + 体系对口」。

    为什么必须有这一层：无负极是**电芯构型**，往往根本不是论文的研究重点
    （比如“电解液工程 + 裸 Cu 集流体直接沉积”），标题里可能一个字都没写，
    关键词表对这类论文完全无计可施。

    风险同样明确：网上多写一句话就是凭空置顶一篇不相关的论文，
    所以**只要体系认不出来就不保底**，宁可漏不可错。
    """

    @staticmethod
    def _plain_work(doi: str = "10.1/ai-keep") -> dict:
        return {"doi": doi, "title": "Non-aqueous cell chemistry", "pub_date": "2026-09-01"}

    def test_ai_detected_anode_free_is_promoted(self):
        def fake_call(work, keywords):
            return 25, "无负极构型下的 Na 沉积行为", "摘要里写了裸 Cu 集流体", True, "sodium"

        with patch.object(ai_matcher, "call_ai", side_effect=fake_call):
            passed, failed, rejected = ai_matcher.evaluate_works([self._plain_work()], threshold=60)

        self.assertEqual([w["doi"] for w in passed], ["10.1/ai-keep"])
        self.assertEqual(failed, [])
        self.assertEqual(rejected, [])
        # 只免阈值，不改分数
        self.assertEqual(passed[0]["ai_score"], 25)
        self.assertEqual(passed[0]["keep_reason"], "无负极（AI 判定 · 钠电）")
        self.assertTrue(passed[0]["anode_free"])

    def test_ai_detected_anode_free_in_another_system_is_not_promoted(self):
        """锂硫那种“顺带提一句无负极”必须卡在门槛外。"""

        def fake_call(work, keywords):
            return 25, "无负极锂硫电池", "锂硫", True, "other"

        with patch.object(ai_matcher, "call_ai", side_effect=fake_call):
            passed, failed, rejected = ai_matcher.evaluate_works([self._plain_work()], threshold=60)

        self.assertEqual(passed, [])
        self.assertEqual(failed, [])
        self.assertEqual([w["doi"] for w in rejected], ["10.1/ai-keep"])

    def test_ai_detected_anode_free_survives_an_ai_failure(self):
        """AI 挂了就没有这个判断，不能凭空保底（这是可以接受的漏）。"""
        with patch.object(ai_matcher, "call_ai", side_effect=RuntimeError("API 挂了")):
            passed, failed, rejected = ai_matcher.evaluate_works([self._plain_work()], threshold=60)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])
        self.assertEqual([w["doi"] for w in failed], ["10.1/ai-keep"])

    def test_ai_keep_reason_survives_the_ranking(self):
        """置顶靠 ranking.annotate，它得认得出 AI 保底（不是只看关键词）。"""
        work = {
            "doi": "x",
            "ai_score": 25,
            "title": "Non-aqueous cell chemistry",
            "anode_free": True,
            "cathode_system": "sodium",
        }
        ranking.rank([work])
        self.assertTrue(work["force_keep"])
        self.assertEqual(work["keep_reason"], "无负极（AI 判定 · 钠电）")

    def test_keyword_keep_wins_over_the_ai_reason(self):
        """两条通道都命中时保留关键词那条理由（它更具体、也更早生效）。"""
        work = {
            "doi": "x",
            "ai_score": 25,
            "title": "Anode-free sodium metal batteries",
            "anode_free": True,
            "cathode_system": "sodium",
        }
        ranking.rank([work])
        self.assertEqual(work["keep_reason"], "无负极")

    def test_ai_keep_needs_a_recognised_system_string(self):
        """认不出来的体系名一律不保底 —— 猜错就是凭空置顶一篇不相关的论文。"""
        work = {"doi": "x", "ai_score": 25, "anode_free": True, "cathode_system": "lithium-sulfur"}
        self.assertIsNone(content_rules.ai_keep_hit(work))
        ranking.rank([work])
        self.assertFalse(work["force_keep"])

    def test_ai_keep_hit_needs_the_flag(self):
        self.assertIsNone(content_rules.ai_keep_hit({"cathode_system": "sodium"}))
        self.assertIsNone(content_rules.ai_keep_hit({"anode_free": False, "cathode_system": "sodium"}))
        self.assertEqual(
            content_rules.ai_keep_hit({"anode_free": True, "cathode_system": "li-rich-mn"}),
            "无负极（AI 判定 · 富锂锰）",
        )

    def test_ai_keep_describe_is_not_polluted(self):
        """describe_keeps 只描述词表规则；AI 那层写在 summary 里，别把两套词表混作一团。"""
        self.assertNotIn("AI 判定", content_rules.describe_keeps())


class TestNormalizeFlags(unittest.TestCase):
    """AI 的结构化判断要能容错解析，而且**认不出来就不保底**。"""

    def test_bool_and_system(self):
        self.assertEqual(
            ai_matcher.normalize_flags({"anode_free": True, "cathode_system": "sodium"}),
            (True, "sodium"),
        )

    def test_string_true_and_alias_system(self):
        self.assertEqual(
            ai_matcher.normalize_flags({"anode_free": "true", "cathode_system": "Li-rich-Mn"}),
            (True, "li-rich-mn"),
        )

    def test_chinese_true_is_accepted(self):
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": "是"})[0], True)

    def test_false_and_missing_fields(self):
        self.assertEqual(ai_matcher.normalize_flags({}), (False, ""))
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": False})[0], False)
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": "false"})[0], False)
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": 0})[0], False)

    def test_garbage_values_do_not_raise(self):
        """模型返回 None / 列表 / 数字都不能把整个流程搞崩。"""
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": None})[0], False)
        self.assertEqual(ai_matcher.normalize_flags({"anode_free": []})[0], False)
        self.assertEqual(ai_matcher.normalize_flags({"cathode_system": None})[1], "")
        self.assertEqual(ai_matcher.normalize_flags({"cathode_system": 3})[1], "")

    def test_unknown_system_is_blanked(self):
        self.assertEqual(
            ai_matcher.normalize_flags({"anode_free": True, "cathode_system": "谁知道呢"}),
            (True, ""),
        )

    def test_off_topic_systems_are_other_never_a_kept_system(self):
        """锂硫这类体系写明是 "other"（比留空更能表达意图），关键是**绝不能**落到保底档。"""
        for raw in ("锂硫", "Li-S", "lithium sulfur", "三元", "磷酸铁锂"):
            with self.subTest(raw=raw):
                resolved = ai_matcher.normalize_flags({"cathode_system": raw})[1]
                self.assertEqual(resolved, "other", raw)
                self.assertNotIn(resolved, config.KEEP_CATHODE_SYSTEMS)

    def test_aliases_resolve_to_the_canonical_id(self):
        for raw, expected in (
            ("lrlo", "li-rich-mn"),
            ("LMR", "li-rich-mn"),
            ("富锂锰", "li-rich-mn"),
            ("Na", "sodium"),
            ("钠离子", "sodium"),
            ("其它", "other"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ai_matcher.normalize_flags({"cathode_system": raw})[1], expected, raw)

    def test_multi_word_aliases_survive_the_key_normalisation(self):
        """★ 真实 bug 的回归：别名表的键必须和查表用的是**同一套**归一化。

        以前手写键用空格（``"lithium rich manganese"``），而查表前空格已经换成了连字符
        （``"lithium-rich-manganese"``）⇒ **所有多词别名静默失效**：
        AI 明明认出了体系、代码却当作"没答"，于是一篇该保底的论文没被保底。
        这个 bug 单看代码完全看不出来，所以每次改别名表都得跑这组用例。
        """
        for raw in (
            "Li-rich",
            "li rich",
            "lithium-rich",
            "lithium rich",
            "lithium rich manganese",
            "Li-excess",
            "Mn-rich",
            "Na-ion",
            "na ion",
            "sodium metal",
            "sodium-ion battery",
            "prussian blue analogue",
        ):
            with self.subTest(raw=raw):
                self.assertNotEqual(ai_matcher.normalize_flags({"cathode_system": raw})[1], "", raw)

    def test_trailing_generic_words_are_trimmed_before_giving_up(self):
        """模型常把体系名写成一整段（``"OLO cathode"``），剥掉尾部通用词后要能落回标准值。"""
        for raw, expected in (
            ("OLO cathode", "li-rich-mn"),
            ("LMR cathode", "li-rich-mn"),
            ("LRLO oxide", "li-rich-mn"),
            ("Li-rich layered oxide cathode", "li-rich-mn"),
            ("sodium layered oxide", "sodium"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ai_matcher.normalize_flags({"cathode_system": raw})[1], expected, raw)

    def test_trailing_trim_never_guesses_from_a_meaningless_stub(self):
        """剥词只能在剥完之后**仍然命中别名**时才算数：不能把 ``"li-rich"`` 削成 ``"li"``。"""
        for raw in ("li", "rich", "cathode", "layered", "NCM811", "材料"):
            with self.subTest(raw=raw):
                self.assertEqual(ai_matcher.normalize_flags({"cathode_system": raw})[1], "", raw)


class TestMailerKeepNotice(unittest.TestCase):
    """页头与卡片都要说明"为什么它排在前面"，否则 AI 55 分排在 95 分前面像 bug。"""

    @staticmethod
    def _html(works: list[dict]) -> str:
        html_body, _ = mailer.build_html(
            ranking.rank(works), "2026-09-10", lookback_days=14, first_run=False
        )
        return html_body

    def test_card_and_header_explain_the_keep(self):
        html_body = self._html(
            [{"doi": "10.1/k", "title": "Anode-free sodium metal battery", "ai_score": 50}]
        )
        self.assertIn("硬保底 · 无负极", html_body)
        self.assertIn("保底规则", html_body)
        self.assertIn("置顶", html_body)

    def test_no_notice_when_nothing_is_kept(self):
        html_body = self._html([{"doi": "10.1/x", "title": "Li-rich cathode work", "ai_score": 80}])
        self.assertNotIn("保底规则", html_body)
        self.assertNotIn("硬保底", html_body)

    def test_plain_text_alternative_lists_the_keep(self):
        work = {"doi": "10.1/k", "title": "Anode-free sodium metal battery", "ai_score": 50}
        _, plain = mailer.build_html(
            ranking.rank([work]), "2026-09-10", lookback_days=14, first_run=False
        )
        self.assertIn("硬保底 · 无负极", plain)


class TestPdfKeepMarker(unittest.TestCase):
    """排进附件的保底论文也要标出来（无负极论文特别多时确实会溢出到附件）。"""

    def test_forced_paper_is_marked_in_the_pdf(self):
        from src import pdf_report

        work = {
            "doi": "10.1/k",
            "title": "Anode-free sodium metal battery",
            "journal": "JACS",
            "pub_date": "2026-09-01",
            "ai_score": 50,
        }
        ranking.annotate([work])
        blocks = pdf_report._item_blocks(work, 1)
        texts = [str(text) for _style, text in blocks]
        self.assertTrue(any("硬保底" in text for text in texts), texts)
        # 不能用 emoji：PDF 走 Adobe-GB1，emoji 会被 gbk_safe() 悄悄丢掉
        self.assertNotIn("🎯", "".join(texts))


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

        def fake_get(url, params=None, headers=None, timeout=None):
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


class TestCrossrefAbstractRetry(unittest.TestCase):
    """摘要回退这一路也必须重试。

    实测日志：``Crossref 无摘要 10.1038/s41467-026-75101-w (HTTP 429)`` ——
    逐刊检索刚打完 15 本刊，紧接着逐篇查摘要就被限流了。
    摘要是 AI 打分的主要依据，一次 429 不该让这篇文献退化成"仅看标题"。
    """

    class _Resp:
        def __init__(self, status_code=200, payload=None, headers=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}

        def json(self):
            return self._payload

    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    @staticmethod
    def _reset() -> None:
        abstract_source._crossref_state["disabled"] = False
        abstract_source._crossref_state["streak"] = 0

    def _run(self, codes, payload=None):
        """``codes`` 是依次返回的状态码，用完后一直返回 200。"""
        seen: list[dict] = []
        queue = list(codes)

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.append({"url": url, "params": params or {}, "headers": headers or {}})
            code = queue.pop(0) if queue else 200
            return self._Resp(code, payload or {"message": {"abstract": "<jats:p>An abstract.</jats:p>"}})

        with patch.object(abstract_source.requests, "get", side_effect=fake_get), \
                patch.object(abstract_source.time, "sleep", lambda _s: None):
            text = abstract_source.from_crossref("10.1/paper")
        return text, seen

    def test_429_is_retried_instead_of_losing_the_abstract(self):
        text, seen = self._run([429, 429])
        self.assertEqual(text, "An abstract.")
        self.assertEqual(len(seen), 3)
        self.assertEqual(abstract_source._crossref_state["streak"], 0)

    def test_client_errors_are_not_retried(self):
        """404 = 该 DOI 在 Crossref 无记录，重试多少次都一样。"""
        text, seen = self._run([404])
        self.assertEqual(text, "")
        self.assertEqual(len(seen), 1)
        self.assertEqual(abstract_source._crossref_state["streak"], 0)

    def test_the_polite_pool_mailto_comes_from_crossref_mailto(self):
        """原来用的是 OPENALEX_MAILTO（只来自 SMTP_USER，本地为空）⇒ 不在礼貌池。"""
        with patch.object(abstract_source, "CROSSREF_MAILTO", "me@example.com"):
            _, seen = self._run([])
        self.assertEqual(seen[0]["params"]["mailto"], "me@example.com")
        self.assertIn("User-Agent", seen[0]["headers"])

    def test_persistent_failure_trips_the_breaker_and_stops_calling(self):
        limit = abstract_source.CROSSREF_CIRCUIT_BREAK_AFTER
        calls = 0

        def fake_get(url, params=None, headers=None, timeout=None):
            nonlocal calls
            calls += 1
            return self._Resp(429)

        with patch.object(abstract_source.requests, "get", side_effect=fake_get), \
                patch.object(abstract_source.time, "sleep", lambda _s: None):
            for i in range(limit + 3):
                self.assertEqual(abstract_source.from_crossref(f"10.1/paper{i}"), "")

        self.assertEqual(calls, limit * abstract_source.CROSSREF_RETRIES)
        self.assertTrue(abstract_source._crossref_state["disabled"])

    def test_each_level_has_its_own_breaker(self):
        """Crossref 熔断不该连累 S2 —— 它只是降级链路的上游，不是同一个池子。"""
        abstract_source._crossref_state["disabled"] = True
        urls: list[str] = []
        ok = self._Resp(200, {"abstract": "An abstract."})

        def fake_get(url, params=None, headers=None, timeout=None):
            urls.append(url)
            return ok

        with patch.object(abstract_source.requests, "get", side_effect=fake_get), \
                patch.object(abstract_source.time, "sleep", lambda _s: None):
            self.assertEqual(abstract_source.from_crossref("10.1/paper"), "")
            self.assertEqual(abstract_source.from_semantic_scholar("10.1/paper"), "An abstract.")

        self.assertEqual(len(urls), 1)  # 熔断后一篇 Crossref 都不发
        self.assertIn("semanticscholar", urls[0])


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
        self.attachments: list[tuple[str, bytes] | None] = []
        self.fetched: list[str] = []
        # 多源是 main 的默认行为 —— 不把备用源 stub 掉，单测就会真的去
        # 请求 Crossref / Semantic Scholar（慢、依赖网络、还会被限流）。
        self._saved_adapters = dict(source_layer.ADAPTERS)
        self.addCleanup(self._restore_adapters)
        source_layer.ADAPTERS["crossref"] = self._no_backup
        source_layer.ADAPTERS["semantic_scholar"] = self._no_backup

    def _restore_adapters(self):
        source_layer.ADAPTERS.clear()
        source_layer.ADAPTERS.update(self._saved_adapters)

    def tearDown(self):
        config.PUSHED_FILE = self._orig_file
        config.RESEARCH_TOPICS = self._orig_topics
        self.tmp.cleanup()

    # -- 桩 -------------------------------------------------------------
    @staticmethod
    def _no_backup(terms, lookback_days, max_works=None, **_kw):
        """备用源在单测里绝不联网：返回空 = 参与了但没命中。"""
        return []

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

    def _fake_send(self, subject, html_body, plain_body, recipients=None, attachment=None):
        """``send_mail`` 的桩。

        ``attachment`` 必须是 keyword-only 带默认值的参数 —— 否则以后给
        ``send_mail`` 再加参数时，这里会静默变成"参数个数不对"的失败。
        """
        self.sent.append((subject, html_body))
        self.attachments.append(attachment)

    def _patches(self):
        return [
            patch.object(main_module, "validate_env", lambda **_kw: None),
            patch.object(main_module.openalex_client, "fetch_works", side_effect=self._fake_fetch),
            patch.object(main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works),
            patch.object(main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval),
            patch.object(main_module.mailer, "send_mail", side_effect=self._fake_send),
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

        def boom_backup(*_a, **_k):
            raise RuntimeError("备用源也挂了")

        args = main_module.build_parser().parse_args([])
        with patch.object(main_module, "validate_env", lambda **_kw: None), patch.object(
            main_module.openalex_client, "fetch_works", side_effect=flaky_fetch
        ), patch.dict(
            # 三源全挂才会让主题失败；只挂主源时备用源会把它救回来（这正是多源的意义）
            source_layer.ADAPTERS,
            {"crossref": boom_backup, "semantic_scholar": boom_backup},
        ), patch.object(
            main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works
        ), patch.object(
            main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval
        ), patch.object(
            main_module.mailer, "send_mail", side_effect=self._fake_send
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
            main_module.mailer, "send_mail", side_effect=self._fake_send
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
        self.assertIn("无负极 +10", text)
        self.assertIn("剔除规则（全局）", text)
        self.assertIn("电解液工程", text)
        self.assertIn("隔膜改性", text)
        # 保底规则必须能离线看到，否则老板要盯的方向配错了也发现不了
        self.assertIn("硬保底（全局）", text)
        self.assertIn("免剔除规则、免 AI 入选线", text)

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
    """内容加分：固态电池 +1、固态聚合物电解质再 +1、无负极 +10。

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

    def test_anode_free_carries_the_full_weight(self):
        """无负极是重点关注方向，权重比其他加分高一个数量级（+10）。"""
        work = {"title": "Anode-free sodium metal batteries"}
        self.assertEqual(content_rules.bonus_score(work), 10)

    def test_anode_free_spellings_are_all_recognised(self):
        """连字符/有无连字符/"free anode"倒装/常见缩写都要能命中。

        注意这里只查**加分**：加分只管排序，不看体系（体系闸门在 KEEP_RULES 那边，
        见 TestKeepRules），所以这 8 个写法不管体系都要 +10。
        """
        for title in (
            "Anode-free lithium metal batteries",
            "Anodeless sodium battery",
            "Anode less configuration for Li metal",
            "Li-free anode design",
            "Na-free anode",
            "Zero-excess sodium metal battery",
            "Hostless metal deposition",
            "AFLMB with a high-voltage cathode",
        ):
            with self.subTest(title=title):
                self.assertEqual(content_rules.bonus_score({"title": title}), 10, title)

    def test_anode_free_stacks_on_top_of_other_bonuses(self):
        """无负极 + 固态体系：两条加分叠加（10 + 1）。"""
        self.assertEqual(content_rules.bonus_score({"title": "Zero-excess all-solid-state batteries"}), 11)

    def test_bonus_can_be_triggered_by_the_abstract(self):
        """加分是"排序依据"，宁滥勿缺：摘要里提到也算。"""
        work = {"title": "A new cathode design", "abstract": "assembled in an anode-free cell"}
        self.assertEqual(content_rules.bonus_score(work), 10)

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

    def test_rule_matching_supports_require_groups(self):
        """``require`` 是"必须命中"的短语**组**：组间 AND、组内 OR。

        为什么需要它：``all`` 表达不了"A 或 B 至少命中一个"，而保底恰恰要的是
        无负极 **且**（富锂锰 **或** 钠电）。
        """
        rule = {"any": ["anode free"], "require": [["li rich", "lithium rich"], ["solid"]]}
        norm = content_rules.normalize
        self.assertTrue(content_rules.rule_matches(rule, norm("anode-free Li-rich solid cells")))
        # 组内 OR：换成同组另一个写法照样命中
        self.assertTrue(content_rules.rule_matches(rule, norm("anode-free lithium rich solid cells")))
        # 组间 AND：第二组（solid）没命中 → 不匹配
        self.assertFalse(content_rules.rule_matches(rule, norm("anode-free Li-rich cells")))
        # require 为空 = 不限制
        self.assertTrue(content_rules.rule_matches({"any": ["anode free"]}, norm("anode-free")))


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


# ★ Seg Q 事故的**原始论文**（用户提供，不是编的）：无负极锂硫被保底规则无条件置顶。
#   Chao Ding et al., Angewandte Chemie Int. Ed., DOI 10.1002/anie.2370748。
#   它的价值在于：`anode free` 词表**真的命中**（所以光靠词表必漏），
#   拦住它的是后来加的**体系闸门**（正极是 Li2S 锂硫，不是富锂锰/钠电）。
LI_S_ANODE_FREE_TITLE = (
    "A Sphere-Sheet Hetero-Interlayer With Mechanoadaptivity and Li+ Selectivity "
    "for High Performance Anode Free Lithium Sulfur Batteries"
)
LI_S_ANODE_FREE_ABSTRACT = (
    "Anode-free lithium sulfur batteries pair a Li2S cathode with a bare copper current "
    "collector. The sphere-sheet hetero-interlayer provides mechanoadaptivity and Li+ "
    "selectivity, suppressing dendrite growth and polysulfide shuttling."
)
LI_S_ANODE_FREE_WORK = {
    "title": LI_S_ANODE_FREE_TITLE,
    "abstract": LI_S_ANODE_FREE_ABSTRACT,
    "doi": "10.1002/anie.2370748",
}


def li_s_anode_free_work(**extra) -> dict:
    """真实锂硫无负极论文的 work dict（``**extra`` 用来叠加 AI 判定字段）。"""
    return dict(LI_S_ANODE_FREE_WORK, **extra)


#: 故意**只**在召回层、不进保底闸门的词（见
#: ``TestKeepRules.test_every_recall_term_can_also_pass_the_gate``）。
#:   * ``anode-free lithium``：它是「构型词」不是「体系词」，闸门的职责正是筛体系；
#:   * ``voltage hysteresis``：锂硫论文篇篇都写，当体系证据会把锂硫放回置顶。
GATE_EXEMPT_RECALL_TERMS = {"anode-free lithium", "voltage hysteresis"}


class TestKeepRules(unittest.TestCase):
    """硬保底：命中即强制进邮件 —— 免于剔除规则、也免于 AI 入选线。

    这一层的失效方式是**静默地丢文献**（规则不命中 → 什么都不报，文献就是没来），
    所以每条路径都得钉住，包括"不能把剔除规则整体放开"这个反向约束。
    起因：一篇无负极的钠电电解液 JACS 被「电解液工程」剔除规则杀掉了。
    """

    def test_anode_free_is_kept(self):
        work = {"title": "Anode-free sodium metal batteries"}
        self.assertEqual(content_rules.keep_hit(work), "无负极")
        self.assertTrue(content_rules.is_kept(work))

    def test_ordinary_paper_is_not_kept(self):
        work = {"title": "Anionic redox in Li-rich layered oxides"}
        self.assertIsNone(content_rules.keep_hit(work))
        self.assertFalse(content_rules.is_kept(work))

    def test_keep_can_be_triggered_by_the_abstract(self):
        """保底范围是标题+摘要（KEEP_SCOPE）：摘要里提到同样算这个方向。"""
        work = {"title": "A new cathode design", "abstract": "cycled in an anode-free sodium cell"}
        self.assertEqual(content_rules.keep_hit(work), "无负极")

    def test_keep_scope_can_be_narrowed_to_the_title(self):
        work = {"title": "Electrolyte engineering for sodium batteries", "abstract": "anode-free"}
        self.assertIsNotNone(content_rules.keep_hit(work))
        with patch.object(config, "KEEP_RULES", [dict(config.KEEP_RULES[0], scope="title")]):
            self.assertIsNone(content_rules.keep_hit(work))

    def test_anode_free_electrolyte_paper_survives_partitioning(self):
        """★ 核心回归：无负极 + 电解液工程 的论文必须活下来。"""
        work = {"title": "Anode-free sodium metal batteries enabled by electrolyte engineering"}
        kept, dropped = content_rules.partition_excluded([work])
        self.assertEqual(dropped, [])
        self.assertEqual([w["title"] for w in kept], [work["title"]])
        self.assertEqual(work["keep_reason"], "无负极")
        # 不得同时留下剔除原因：页头/日志会据此报"剔除 N 篇"，两边都写就自相矛盾
        self.assertNotIn("exclude_reason", work)

    def test_plain_electrolyte_paper_is_still_dropped(self):
        """反向约束：保底不能把「电解液工程」这条剔除规则整体废掉。"""
        work = {"title": "Electrolyte additives for high-voltage lithium batteries"}
        kept, dropped = content_rules.partition_excluded([work])
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["exclude_reason"], "电解液工程")

    def test_exclusion_hit_still_reports_none_for_a_kept_paper(self):
        """免剔除做在 ``exclusion_hit`` 里，所以任何调用点都绕不过去。"""
        title = "Anode-free sodium metal batteries via electrolyte engineering"
        self.assertIsNotNone(content_rules.exclusion_hit({"title": "Electrolyte engineering"}))
        self.assertIsNone(content_rules.exclusion_hit({"title": title}))

    def test_topic_keep_is_scoped(self):
        """主题自己的 keep 只对该主题生效，全局的照常叠加。"""
        scoped = config.ResearchTopic(
            name="A", keywords=["x"], keep=[{"label": "本主题必留", "any": ["prussian blue"]}]
        )
        work = {"title": "Prussian blue analogue cathode"}
        self.assertEqual(content_rules.keep_hit(work, scoped), "本主题必留")
        self.assertIsNone(content_rules.keep_hit(work))

    def test_mark_kept_writes_the_reason(self):
        works = [{"title": "Anode-free sodium battery"}, {"title": "unrelated"}]
        marked = content_rules.mark_kept(works)
        self.assertEqual([w["title"] for w in marked], ["Anode-free sodium battery"])
        self.assertEqual(works[0]["keep_reason"], "无负极")
        self.assertNotIn("keep_reason", works[1])

    def test_describe_keeps_lists_the_trigger_phrases(self):
        """``--show-config`` 与启动日志靠它回显，写错了才看得出来。"""
        text = content_rules.describe_keeps()
        self.assertIn("无负极", text)
        self.assertIn("anode free", text)
        # ★ 体系闸门也要回显："保底为什么不触发"最常见的原因就是闸门没命中
        self.assertIn("且须命中", text)

    def test_anode_free_spellings_are_all_recognised_on_the_keep_path(self):
        """保底这条通道上，各种写法同样都要能命中（题目都带体系词）。"""
        for title in (
            "Anode-free sodium metal batteries",
            "Anodeless Na-ion battery",
            "Anode less configuration for Na metal",
            "Li-free anode for Li-rich cathodes",
            "Na-free anode in sodium cells",
            "Zero-excess sodium metal battery",
            "Hostless sodium deposition",
            "AFLMB with a Li-rich cathode",
        ):
            with self.subTest(title=title):
                self.assertEqual(content_rules.keep_hit({"title": title}), "无负极", title)

    def test_anode_free_without_a_matching_cathode_system_is_not_kept(self):
        """★ 核心回归（Seg Q）：只看「无负极」会把**不属于本课题**的论文也强推进邮件。

        起因：一篇锂硫论文的摘要里只有一句 "comparable to anode-free lithium metal
        batteries"，就命中了保底 → 免剔除 + 免 AI 阈值 + **排到邮件最上面**，
        而锂硫跟富锂锰 / 钠电正极毫无关系。
        """
        work = {
            "title": "High areal capacity lithium-sulfur batteries",
            "abstract": "performance comparable to anode-free lithium metal batteries",
        }
        self.assertIsNone(content_rules.keep_hit(work))
        self.assertFalse(content_rules.is_kept(work))
        # 加分照给：加分只管排序，体系闸门只在"放不放它进来"这一层
        self.assertEqual(content_rules.bonus_score(work), 10)

    def test_anode_free_plus_each_cathode_system_is_kept(self):
        """两个体系（富锂锰 / 钠电）任一对口都要保底 —— 闸门是 OR 不是 AND。"""
        for title in (
            "Anode-free Li-rich layered oxide cathodes",
            "Zero-excess sodium metal batteries",
            "Anode-free Na-ion full cells",
            "Anode-free batteries with Li2MnO3-based cathodes",
            "Hostless deposition in LMR cathodes",
        ):
            with self.subTest(title=title):
                self.assertEqual(content_rules.keep_hit({"title": title}), "无负极", title)

    def test_the_gate_does_not_fire_on_an_off_topic_system(self):
        """锂硫 / 磷酸铁锂 / 富镍这些体系不进保底。"""
        for title in (
            "Anode-free lithium-sulfur batteries",
            "Anode-free LiFePO4 batteries",
            "Anode-free nickel-rich NCM cathodes",
        ):
            with self.subTest(title=title):
                self.assertIsNone(content_rules.keep_hit({"title": title}), title)

    def test_gate_recognises_li_rich_papers_that_never_say_li_rich(self):
        """★ 闸门的**漏报**方向（比误招更严重）。

        富锂锰论文经常一个 "Li-rich" 都不写：只写 OLO / 化学式 / 机理词。
        以前这些写法闸门全都认不出 → 召回捞进来了、保底却失效，
        正好漏掉最该留的论文。用户口径：宁可多留，绝不能漏。
        """
        for title in (
            "Anode-free cells with an over-lithiated layered oxide cathode",
            "Anode-free batteries based on Li1.2Mn0.54Ni0.13Co0.13O2",
            "Anode-free full cell using an OLO cathode",
            "Zero-excess cell showing reversible oxygen redox",
            "Hostless configuration with an anionic redox cathode",
            "Anode-free cells with suppressed voltage decay",
            "Anode-free cell with excess lithium in the cathode",
            "Anode-free cell with a cation-disordered Li-excess oxide",
        ):
            with self.subTest(title=title):
                self.assertEqual(content_rules.keep_hit({"title": title}), "无负极", title)

    def test_gate_recognises_sodium_papers_written_with_the_na_prefix(self):
        """钠电同上：摘要里常把 sodium 写成 Na（化学式 / Na layered oxide）。"""
        for title in (
            "Anode-free cell with a Na0.67MnO2 cathode",
            "Anode-free cell with a Na layered oxide",
            "Anode-free cell with a Na excess cathode",
            "Anodeless configuration using a prussian blue analogue",
        ):
            with self.subTest(title=title):
                self.assertEqual(content_rules.keep_hit({"title": title}), "无负极", title)

    def test_voltage_hysteresis_alone_still_does_not_keep_a_lithium_sulfur_paper(self):
        """⚠️ ``voltage hysteresis`` 是富锂锰的召回词，却**故意**不进闸门。

        锂硫论文几乎篇篇都把 "severe voltage hysteresis" 当缺点写，
        一旦把它当体系证据，锂硫 + 无负极就会重新混进置顶（正是要拦住的那一类）。
        """
        work = {
            "title": "Anode free lithium sulfur batteries with severe voltage hysteresis"
        }
        self.assertIsNone(content_rules.keep_hit(work))
        self.assertNotIn("voltage hysteresis", config.ANODE_FREE_KEEP_CATHODES)

    def test_every_recall_term_can_also_pass_the_gate(self):
        """★ 自维护的「不漏」保险：召回层能捞到的词，闸门必须也认。

        两层词表指向同一批论文：**召回层捞得到的，就是闸门该保底的**。
        如果新加了一个召回词却没加进闸门，就会出现最隐蔽的漏报 ——
        论文进了候选池、AI 也打了分，却拿不到保底（日志里看不出任何异常）。
        这里逐词测：``Anode-free cell with <召回词>`` 必须能保底。
        例外只能写在 ``GATE_EXEMPT_RECALL_TERMS`` 里，改一处就必须改这里。
        """
        for topic in config.active_research_topics():
            for term in topic.search_terms:
                if term in GATE_EXEMPT_RECALL_TERMS:
                    continue
                with self.subTest(topic=topic.name, term=term):
                    work = {"title": f"Anode-free cell with {term}"}
                    self.assertEqual(content_rules.keep_hit(work, topic), "无负极", term)

    def test_real_li_s_anode_free_paper_is_blocked_by_the_gate(self):
        """★ 核心回归（Seg Q 的**真实**触发案例，标题与 DOI 是用户给的原文）。

        这篇论文的标题里明明白白写着 `Anode Free Lithium Sulfur`：
        词表那侧的 `anode free` **确实命中** —— 也就是说，只写「无负极」的保底
        会把一篇跟富锂锰 / 钠电正极毫无关系的锂硫论文**免剔除 + 免阈值 + 置顶**。
        拦住它的是**体系闸门**，不是词没写全。
        """
        work = li_s_anode_free_work()
        self.assertIsNone(content_rules.keep_hit(work))
        self.assertFalse(content_rules.is_kept(work))

        # 证明拦住它的**确实是闸门**：把闸门摘掉，同一篇立刻被保底
        mine = [dict(rule, require=None) for rule in config.KEEP_RULES]
        with patch.object(config, "KEEP_RULES", mine):
            self.assertEqual(content_rules.keep_hit(work), "无负极")

    def test_real_li_s_paper_still_gets_the_sorting_bonus(self):
        """加分只挂「无负极」、故意不带闸门 —— 所以它照拿 +10，只是不会被置顶/免阈值。"""
        work = li_s_anode_free_work()
        self.assertEqual(content_rules.bonus_score(work), 10)
        self.assertEqual(content_rules.matched_bonuses(work), [("无负极", 10)])

    def test_real_li_s_paper_is_not_kept_by_the_ai_channel_either(self):
        """AI 通道同样拦住它：它**确实是**无负极，但正极是 Li2S ⇒ `cathode_system="other"`。

        这就是那条「宁可漏、不能错」的纪律：认不出来 / 对不上体系，一律不保底。
        """
        work = li_s_anode_free_work(anode_free=True, cathode_system="other")
        self.assertIsNone(content_rules.ai_keep_hit(work))
        # 缺字段（AI 挂了 / 没返回）也只失去这条路，不会误保底
        self.assertIsNone(content_rules.ai_keep_hit(li_s_anode_free_work(anode_free=True)))

    def test_summary_mentions_the_keep_layer(self):
        self.assertIn("硬保底", content_rules.summary())


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

    def test_broken_keep_rule_is_reported(self):
        """保底规则配错只会静默失效，所以必须在校验里报出来。"""
        with patch.object(config, "KEEP_RULES", [{"label": "坏的", "any": []}]):
            problems = config.validate_content_rules()
        self.assertTrue(any("KEEP_RULES" in p and "永远不会生效" in p for p in problems), problems)

    def test_broken_require_shape_is_reported(self):
        """``require`` 写成字符串（漏一层方括号）会让规则**永久不命中**，必须报出来。"""
        broken = [{"label": "坏的", "any": ["x"], "require": "sodium"}]
        with patch.object(config, "KEEP_RULES", broken):
            problems = config.validate_content_rules()
        self.assertTrue(any('"require"' in p for p in problems), problems)

    def test_empty_require_group_is_reported(self):
        """空组 = 这一组永远不满足 → 整条规则永远不生效。"""
        broken = [{"label": "坏的", "any": ["x"], "require": [["sodium"], []]}]
        with patch.object(config, "KEEP_RULES", broken):
            problems = config.validate_content_rules()
        self.assertTrue(any("永远不会生效" in p for p in problems), problems)

    def test_live_keep_rule_carries_the_cathode_gate(self):
        """体系闸门是这个项目的核心防线，配置里不能悄悄丢掉。"""
        self.assertTrue(config.KEEP_RULES[0].get("require"))
        for rule in config.KEEP_RULES:
            self.assertTrue(rule["require"][0], rule.get("label"))

    def test_gate_terms_actually_match_the_live_config(self):
        """闸门里的词得真能命中文字（改错字就会静默失效）。"""
        for phrase in ("Li-rich layered oxide cathode", "sodium metal battery"):
            with self.subTest(phrase=phrase):
                work = {"title": f"Anode-free cell with {phrase}"}
                self.assertEqual(content_rules.keep_hit(work), "无负极", phrase)

    def test_topic_keep_rule_is_validated(self):
        broken = config.ResearchTopic(name="A", keywords=["x"], keep=[{"label": "坏的", "any": []}])
        problems = config.topic_warnings("topic", broken)
        self.assertTrue(any("keep" in p and "永远不会生效" in p for p in problems), problems)


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
        self.assertEqual(work["content_bonus"], 11)  # 固态 1 + 无负极 10
        self.assertEqual(work["final_score"], 90)
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
            "content_bonus": 11,
            "content_bonus_detail": [["固态电池", 1], ["无负极", 10]],
            "final_score": 81,
        }
        self.assertEqual(ranking.breakdown(work), "AI 70 + 固态电池 1 + 无负极 10 = 81")

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
            "title": "Anode-free solid-state battery with a sodium cathode and a polymer electrolyte",
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
        # 70（AI）+ 7（Joule）+ 1（固态）+ 10（无负极）+ 1（固态聚合物电解质）
        self.assertIn("最终 89 分", html_body)
        self.assertIn("Joule +7（AI 70）", html_body)
        self.assertIn("固态电池 +1", html_body)
        self.assertIn("无负极 +10", html_body)
        self.assertIn("硬保底 · 无负极", html_body)
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
        self.assertEqual(scores["无负极"], 10)

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

    def test_keep_rule_has_the_cathode_gate(self):
        """★ Seg Q：无负极保底必须带体系闸门（锂硫事件后加的）。"""
        self.assertEqual(config.KEEP_RULES[0]["label"], "无负极")
        gate = config.KEEP_RULES[0]["require"][0]
        for term in ("li rich", "sodium"):
            self.assertIn(term, gate)
        # 加分规则**故意**不带闸门（加分只管排序，管不了入选）
        bonus = next(rule for rule in config.BONUS_RULES if rule["label"] == "无负极")
        self.assertNotIn("require", bonus)

    def test_ai_hints_name_the_kept_systems(self):
        """AI 口径提示词里要同时点名两个体系，否则模型不会往那里想。"""
        self.assertIn("无负极", config.AI_ANODE_FREE_HINT)
        self.assertIn("富锂锰", config.AI_CATHODE_HINT)
        self.assertIn("钠", config.AI_CATHODE_HINT)
        self.assertEqual(config.KEEP_CATHODE_SYSTEMS["li-rich-mn"], "富锂锰")
        self.assertEqual(config.KEEP_CATHODE_SYSTEMS["sodium"], "钠电")

    def test_lithium_topic_recalls_anode_free_lithium_papers(self):
        """召回层补词：无负极论文的标题里往往一个原召回词都没有。"""
        lithium = next(t for t in config.active_research_topics() if t.name == "富锂锰正极")
        self.assertIn("anode-free lithium", lithium.search_terms)

    def test_real_li_s_paper_is_recalled_but_never_kept(self):
        """★ 真实案例（DOI 10.1002/anie.2370748，用户提供）：

        `anode-free lithium` 这个词**会把锂硫也捞进候选池** —— 这是召回层
        「宁滥勿缺」的已知代价（多花一次 AI 调用，AI 给低分就淘汰了）。
        所以真正必须钉住的不是召回层的精度，而是**保底的体系闸门**：
        进了池子也绝不能被置顶。
        """
        work = li_s_anode_free_work()
        lithium = next(t for t in config.active_research_topics() if t.name == "富锂锰正极")
        self.assertIn(
            "anode-free lithium", source_base.matches_recall_terms(work, lithium.search_terms)
        )
        self.assertIsNone(content_rules.keep_hit(work, lithium))
        self.assertEqual(content_rules.exclusion_hit(work, lithium), None)

    def test_sodium_topic_recalls_anode_free_sodium_papers(self):
        """★ 真实例子：《Data-Driven Knowledge Discovery Reveals Quantitative
        Electrolyte Design Rules for Anode-Free Sodium Metal Batteries》
        （JACS 2026, 148(30) 31918，DOI 10.1021/jacs.6c05130）——
        在这批补词之前，它在**召回阶段**就被丢了（真实标题里一个词都不命中）。
        """
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        for term in ("sodium metal battery", "anode-free sodium"):
            self.assertIn(term, sodium.search_terms)

    def test_real_jacs_anode_free_sodium_paper_passes_every_layer(self):
        """★ 真实论文全链路回归：用**真实标题**钉住召回→剔除→保底→加分四层。

        DOI ``10.1021/jacs.6c05130`` 是「补召回词」这件事的原始样本。它后来**仍然没被
        推送**，原因不在规则层，而在时间窗（周更是 14 天增量窗口，它 online 于 2026-07-21）——
        所以这里只用真实标题证明**规则层已经不漏**，窗口问题由 ``--lookback-days`` 解决。
        """
        work = {
            "doi": "10.1021/jacs.6c05130",
            "title": (
                "Data-Driven Knowledge Discovery Reveals Quantitative Electrolyte Design "
                "Rules for Anode-Free Sodium Metal Batteries"
            ),
        }
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        self.assertTrue(source_base.matches_recall_terms(work, sodium.search_terms))
        self.assertIsNone(content_rules.exclusion_hit(work, sodium))
        self.assertEqual(content_rules.keep_hit(work, sodium), "无负极")
        self.assertGreater(content_rules.bonus_score(work), 0)

    def test_topic_descriptions_mention_the_anode_free_configuration(self):
        """描述会拼进 AI 的判据，无负极构型得写进去（否则 AI 不会往那儿看）。"""
        sodium = next(t for t in config.active_research_topics() if t.name == "钠离子正极")
        self.assertIn("无负极", sodium.description)


class TestSourceRegistry(unittest.TestCase):
    """数据源名册：别名归一化、打错字必须报错、启用列表去重且保序。"""

    def test_aliases_normalise(self):
        cases = {
            "openalex": "openalex",
            "OA": "openalex",
            "open_alex": "openalex",
            "cr": "crossref",
            "cross-ref": "crossref",
            "crossref": "crossref",
            "s2": "semantic_scholar",
            "semantic-scholar": "semantic_scholar",
            "SemanticScholar": "semantic_scholar",
        }
        for raw, expected in cases.items():
            self.assertEqual(source_base.canonical_source(raw), expected, raw)

    def test_unknown_source_raises_instead_of_being_silently_ignored(self):
        """--sources 打错字若被忽略，用户会以为换了源其实没换成 —— 必须炸。"""
        with self.assertRaises(ValueError) as ctx:
            source_base.canonical_source("openalx")
        self.assertIn("openalex", str(ctx.exception))  # 报错里要顺带给出可用值

    def test_enabled_sources_dedupes_and_keeps_order(self):
        self.assertEqual(
            source_layer.enabled_sources("crossref, s2, crossref"),
            ["crossref", "semantic_scholar"],
        )

    def test_empty_selection_raises(self):
        with self.assertRaises(RuntimeError):
            source_layer.enabled_sources("")

    def test_default_selection_follows_config(self):
        self.assertEqual(list(source_layer.enabled_sources()), list(config.DATA_SOURCES))

    def test_labels_are_human_readable(self):
        self.assertEqual(source_base.source_label("s2"), "Semantic Scholar")
        self.assertEqual(source_base.source_label("crossref"), "Crossref")


class TestUnifiedWorkShape(unittest.TestCase):
    """所有源必须产出同一份结构，否则去重、排序、期刊加成都会错。"""

    def test_missing_doi_is_dropped_not_faked(self):
        """没有 DOI 就不能去重、也点不开，宁可丢掉也不能拿标题当键。"""
        with self.assertLogs("src.sources.base", level="WARNING"):
            self.assertIsNone(
                source_base.make_work(doi="", title="No DOI paper", source="openalex")
            )

    def test_shape_supplies_everything_downstream_needs(self):
        work = source_base.make_work(
            doi="https://doi.org/10.1002/ADMA.74958",
            title="T",
            source="semantic_scholar",
            journal="Advanced Materials",
            issn="0935-9648",
            pub_date="2026-03-01",
            cited_by=5,
            abstract="a",
            abstract_from="semantic_scholar",
        )
        self.assertEqual(work["doi"], "10.1002/adma.74958")  # 归一化并小写
        self.assertEqual(work["uid"], "doi:10.1002/adma.74958")
        self.assertEqual(work["doi_url"], "https://doi.org/10.1002/adma.74958")
        self.assertEqual(work["sources"], ["semantic_scholar"])
        for key in ("title", "journal", "issn", "pub_date", "cited_by", "type", "abstract"):
            self.assertIn(key, work)


class TestLocalRecallRecheck(unittest.TestCase):
    """Crossref 的 query.title 是**分词**匹配，必须本地整词复核，否则假阳性大量涌入。

    实测：query.title=lithium-rich 返回的 Advanced Materials 论文里，
    排名靠前的全是锂金属/电解液方向，与富锂锰正极无关。
    """

    def test_phrase_is_matched_whole_and_not_tokenised(self):
        work = {"title": "Lithium Metal Batteries with a Sulfide Electrolyte"}
        self.assertEqual(source_base.matches_recall_terms(work, ["lithium-rich", "li-rich"]), [])

    def test_hits_are_reported_per_term(self):
        work = {"title": "Li-rich layered oxides", "abstract": "anion redox chemistry"}
        self.assertEqual(
            sorted(source_base.matches_recall_terms(work, ["li-rich", "anion redox", "sodium"])),
            ["anion redox", "li-rich"],
        )

    def test_abstract_alone_is_enough(self):
        work = {"title": "A cathode design", "abstract": "anion redox stabilises the lattice"}
        self.assertEqual(source_base.matches_recall_terms(work, ["anion redox"]), ["anion redox"])

    def test_very_short_terms_are_not_usable_for_recheck(self):
        """短词会命中 nanowire 这类无关词，所以本地复核要主动放过它们。"""
        self.assertEqual(source_base.usable_terms(["na", "li", "anion redox"]), ["anion redox"])

    def test_no_usable_term_still_lets_long_terms_through(self):
        self.assertEqual(source_base.usable_terms(["sodium ion", "anion redox"]),
                         ["sodium ion", "anion redox"])


class TestJournalIdentity(unittest.TestCase):
    """S2 的刊名/ISSN 会系统性地错，只能靠 DOI 前缀认刊。"""

    def test_s2_mislabelled_adma_paper_is_rescued_by_the_doi_prefix(self):
        """实测：10.1002/adma.74958 被 S2 标成 'Advances in Materials'（另一本真实期刊）。"""
        self.assertEqual(
            source_base.identify_journal(
                doi="10.1002/adma.74958",
                venue="Advances in Materials",
                aliases=config.S2_VENUE_ALIASES,
            ),
            "Advanced Materials",
        )

    def test_venue_fallback_handles_html_escaped_ampersand(self):
        """S2 返回的刊名是 HTML 转义的（ENERGY &amp; ENVIRONMENTAL ...）。"""
        self.assertEqual(
            source_base.identify_journal(
                doi="", venue="Energy &amp; Environmental Science", aliases={}
            ),
            "Energy & Environmental Science",
        )

    def test_alias_table_handles_angewandte_variants(self):
        for venue in ("Angewandte Chemie", "Angewandte Chemie International Edition"):
            self.assertEqual(
                source_base.identify_journal(
                    doi="", venue=venue, aliases=config.S2_VENUE_ALIASES
                ),
                "Angewandte Chemie Int. Ed.",
                venue,
            )

    def test_unrecognised_venue_returns_empty_so_the_caller_can_warn(self):
        self.assertEqual(
            source_base.identify_journal(doi="10.9999/x", venue="Some Journal", aliases={}), ""
        )

    def test_every_configured_journal_has_a_doi_pattern_entry(self):
        for name in config.JOURNALS:
            self.assertIn(name, config.JOURNAL_DOI_PATTERNS)

    def test_ees_is_the_only_journal_without_a_doi_prefix(self):
        """RSC 的 DOI 里没有刊名（10.1039/D6EE01234A），所以 EES 只能靠名字兜底。"""
        empty = [n for n, patterns in config.JOURNAL_DOI_PATTERNS.items() if not patterns]
        self.assertEqual(empty, ["Energy & Environmental Science"])


class TestUnionMerge(unittest.TestCase):
    """并集合并：按 DOI 去重、取最长摘要、来源累加、按日期倒序。"""

    def _work(self, **overrides):
        work = {
            "uid": "doi:10.1/a", "doi": "10.1/a", "title": "T", "journal": "J",
            "issn": "", "pub_date": "2026-03-01", "cited_by": 0, "abstract": "",
            "abstract_from": "", "sources": ["openalex"],
        }
        work.update(overrides)
        return work

    def test_same_doi_from_two_sources_becomes_one_entry(self):
        merged, added = source_base.merge_works([
            ("openalex", [self._work()]),
            ("crossref", [self._work(sources=["crossref"])]),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sources"], ["openalex", "crossref"])
        self.assertEqual(added, {"openalex": 1, "crossref": 0})

    def test_longest_abstract_wins(self):
        """并集最实在的收益：同一篇论文常常 OpenAlex 缺摘要而其它源有。"""
        merged, _ = source_base.merge_works([
            ("openalex", [self._work(abstract="short", abstract_from="openalex")]),
            ("crossref", [self._work(
                abstract="a much longer abstract", abstract_from="crossref",
                sources=["crossref"],
            )]),
        ])
        self.assertEqual(merged[0]["abstract"], "a much longer abstract")
        self.assertEqual(merged[0]["abstract_from"], "crossref")

    def test_shorter_abstract_never_overwrites_a_longer_one(self):
        merged, _ = source_base.merge_works([
            ("openalex", [self._work(abstract="a much longer abstract")]),
            ("crossref", [self._work(abstract="short", sources=["crossref"])]),
        ])
        self.assertEqual(merged[0]["abstract"], "a much longer abstract")

    def test_missing_fields_are_backfilled_but_existing_ones_win(self):
        merged, _ = source_base.merge_works([
            ("openalex", [self._work(journal="Advanced Materials", issn="", cited_by=3)]),
            ("crossref", [self._work(
                journal="别的刊", issn="0935-9648", cited_by=99, sources=["crossref"],
            )]),
        ])
        self.assertEqual(merged[0]["journal"], "Advanced Materials")  # 先到的不被覆盖
        self.assertEqual(merged[0]["cited_by"], 3)
        self.assertEqual(merged[0]["issn"], "0935-9648")  # 缺失的补齐

    def test_sorted_newest_first(self):
        merged, _ = source_base.merge_works([("openalex", [
            self._work(uid="doi:1", doi="1", pub_date="2026-01-01"),
            self._work(uid="doi:3", doi="3", pub_date="2026-03-01"),
            self._work(uid="doi:2", doi="2", pub_date="2026-02-01"),
        ])])
        self.assertEqual(
            [w["pub_date"] for w in merged], ["2026-03-01", "2026-02-01", "2026-01-01"]
        )

    def test_entries_without_uid_still_dedupe_by_doi(self):
        merged, _ = source_base.merge_works([
            ("openalex", [{"doi": "10.1/a", "title": "T"}]),
            ("crossref", [{"doi": "10.1/a", "title": "T"}]),
        ])
        self.assertEqual(len(merged), 1)


class TestUnionFetchAggregator(unittest.TestCase):
    """多源并集：单源失败必须活下来，全失败必须报错。"""

    def setUp(self):
        self._saved = dict(source_layer.ADAPTERS)
        self.addCleanup(self._restore)

    def _restore(self):
        source_layer.ADAPTERS.clear()
        source_layer.ADAPTERS.update(self._saved)

    @staticmethod
    def _ok(works):
        return lambda *_a, **_k: [dict(w) for w in works]

    @staticmethod
    def _boom(message="HTTP 500"):
        def _fail(*_a, **_k):
            raise RuntimeError(message)
        return _fail

    @staticmethod
    def _work(doi, **overrides):
        work = source_base.make_work(doi=doi, title="T", source="openalex")
        work.update(overrides)
        return work

    def test_one_failing_source_does_not_kill_the_round(self):
        source_layer.ADAPTERS["openalex"] = self._boom()
        source_layer.ADAPTERS["crossref"] = self._ok([self._work("10.1/a")])
        result = source_layer.fetch_works(["anion redox"], 90, sources="openalex,crossref")
        self.assertEqual(len(result.works), 1)
        self.assertEqual([r.status for r in result.reports], ["failed", "ok"])
        self.assertTrue(result.notices(), "单源故障必须在邮件里提示")

    def test_all_sources_failing_raises(self):
        """绝不能返回空列表假装成功 —— 那正是这个项目吃过的最贵的亏。"""
        for name in ("openalex", "crossref", "semantic_scholar"):
            source_layer.ADAPTERS[name] = self._boom()
        with self.assertRaises(RuntimeError) as ctx:
            source_layer.fetch_works(["anion redox"], 90)
        self.assertIn("所有数据源都失败了", str(ctx.exception))

    def test_running_a_single_source_is_allowed(self):
        source_layer.ADAPTERS["openalex"] = self._boom()
        source_layer.ADAPTERS["crossref"] = self._ok([self._work("10.1/a")])
        result = source_layer.fetch_works(["anion redox"], 90, sources="crossref")
        self.assertEqual(len(result.works), 1)

    def test_keyword_only_sources_are_skipped_not_failed_without_terms(self):
        """topic 模式没有字面词：Crossref/S2 不参与是模式差异，不是故障。"""
        source_layer.ADAPTERS["openalex"] = self._ok([self._work("10.1/a")])
        result = source_layer.fetch_works(
            [], 90, mode="topic", sources="openalex,crossref,semantic_scholar"
        )
        statuses = {r.name: r.status for r in result.reports}
        self.assertEqual(statuses["crossref"], "skipped")
        self.assertEqual(statuses["semantic_scholar"], "skipped")
        self.assertEqual(statuses["openalex"], "ok")
        self.assertFalse(result.notices(), "「跳过」不该被当成故障来吓用户")

    def test_recall_terms_come_from_search_terms_not_only_cli_keywords(self):
        """★ 回归锁：命令行不传 ``--keywords`` 是常态，备用源不能因此被静默跳过。

        曾经这里直接拿 ``keywords`` 参数当召回词 ⇒ 词表恒为空 ⇒
        Crossref/S2 被标成「跳过」⇒ **多源原地失效，而日志看起来一切正常**。
        """
        seen: dict[str, list[str]] = {}

        def _spy(terms, *_a, **_k):
            seen["terms"] = list(terms)
            return []

        source_layer.ADAPTERS["openalex"] = self._ok([])
        source_layer.ADAPTERS["crossref"] = _spy
        topic = config.active_research_topics()[0]

        source_layer.fetch_works(None, 90, topic=topic, sources="crossref")

        self.assertTrue(seen["terms"], "备用源拿到了空词表 ⇒ 多源静默失效")
        self.assertEqual(seen["terms"], list(topic.search_terms))

    def test_duplicates_across_sources_are_merged(self):
        source_layer.ADAPTERS["openalex"] = self._ok([self._work("10.1/a", abstract="short")])
        source_layer.ADAPTERS["crossref"] = self._ok([
            self._work("10.1/a", abstract="a longer abstract", sources=["crossref"])
        ])
        result = source_layer.fetch_works(["anion redox"], 90, sources="openalex,crossref")
        self.assertEqual(len(result.works), 1)
        self.assertEqual(result.works[0]["abstract"], "a longer abstract")

    def test_merged_result_respects_the_global_cap(self):
        source_layer.ADAPTERS["openalex"] = self._ok(
            [self._work(f"10.1/{i}") for i in range(10)]
        )
        source_layer.ADAPTERS["crossref"] = self._ok([])
        result = source_layer.fetch_works(
            ["anion redox"], 90, max_works=4, sources="openalex,crossref"
        )
        self.assertEqual(len(result.works), 4)

    def test_summary_names_every_enabled_source(self):
        source_layer.ADAPTERS["openalex"] = self._ok([self._work("10.1/a")])
        source_layer.ADAPTERS["crossref"] = self._ok([])
        result = source_layer.fetch_works(["anion redox"], 90, sources="openalex,crossref")
        self.assertEqual(result.summary(), "OpenAlex 1 篇 ｜ Crossref 0 篇")

    def test_failure_reason_is_flattened_so_the_header_stays_readable(self):
        """邮件页头是一行设计：OpenAlex 那 5 行额度提示绝不能把正文挤没。"""
        report = source_base.SourceReport(
            name="openalex",
            status="failed",
            error="OpenAlex 今日额度已用完（HTTP 429）。\n  匿名配额 1000 积分/天。\n  彻底解决：注册账号。" * 3,
        )
        line = report.describe()
        self.assertEqual(len(line.splitlines()), 1)
        self.assertLess(len(line), 100)
        self.assertTrue(line.startswith("OpenAlex 失败（"), line)

        skipped = source_base.SourceReport(
            name="crossref", status="skipped", error="该源只支持字面关键词检索，当前模式没有可用召回词"
        )
        self.assertEqual(skipped.describe(), "Crossref 未参与")


class TestCrossrefAdapter(unittest.TestCase):
    """Crossref 的 query.title 是分词匹配，本地复核不是可选项。"""

    class _Resp:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    @staticmethod
    def _item(*, doi, title, abstract=None):
        item = {
            "DOI": doi,
            "title": [title],
            "container-title": ["Whatever Crossref Says"],
            "published": {"date-parts": [[2026, 3, 1]]},
            "type": "journal-article",
            "is-referenced-by-count": 2,
        }
        if abstract:
            item["abstract"] = abstract
        return item

    @staticmethod
    def _payload(items):
        return {"message": {"items": items}}

    def _run(self, items, terms, status_code=200):
        seen: dict = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return self._Resp(self._payload(items), status_code)

        # 重试等待归零：否则「全刊失败」那个用例要真实等 15×6 秒
        with patch.object(crossref_source.requests, "get", side_effect=fake_get), \
                patch.object(config, "CROSSREF_RETRY_WAIT", 0):
            works = crossref_source.fetch(terms, 90)
        return works, seen

    def _run_scripted(self, plan, items, terms):
        """``plan``: {issn: [状态码, ...]}，队列用完则回 200。返回 (works, 实际调用)。"""
        calls: list[tuple[str, int]] = []
        queue = {issn: list(codes) for issn, codes in plan.items()}
        notes: list[str] = []

        def fake_get(url, params=None, headers=None, timeout=None):
            issn = url.split("/journals/", 1)[1].split("/", 1)[0]
            code = queue[issn].pop(0) if queue.get(issn) else 200
            calls.append((issn, code))
            payload = self._payload(items) if code == 200 else {}
            return self._Resp(payload, code)

        with patch.object(crossref_source.requests, "get", side_effect=fake_get), \
                patch.object(config, "CROSSREF_RETRY_WAIT", 0):
            works = crossref_source.fetch(terms, 90, notes=notes)
        return works, calls, notes

    def test_uses_the_per_journal_endpoint_with_an_ors_query(self):
        _, seen = self._run([], ["li-rich", "anion redox"])
        self.assertIn("api.crossref.org/journals/", seen["url"])
        self.assertTrue(seen["url"].endswith("/works"))
        self.assertIn(" OR ", seen["params"]["query.title"])
        self.assertIn('"li-rich"', seen["params"]["query.title"])

    def test_date_and_type_filters_are_sent(self):
        _, seen = self._run([], ["li-rich"])
        self.assertIn("from-pub-date:", seen["params"]["filter"])
        self.assertIn("type:journal-article", seen["params"]["filter"])
        self.assertIn("abstract", seen["params"]["select"])  # 摘要必须要回来

    def test_empty_recall_terms_raise_instead_of_pulling_everything(self):
        """不传 query.title 会把这 15 本刊 90 天的全部论文拉回来。"""
        with self.assertRaises(RuntimeError) as ctx:
            crossref_source.fetch([], 90)
        self.assertIn("召回词", str(ctx.exception))

    def test_tokenised_false_positives_are_dropped_locally(self):
        items = [
            self._item(doi="10.1002/adma.1", title="Lithium Metal Batteries"),
            self._item(doi="10.1002/adma.2", title="Li-rich layered oxide with anion redox"),
        ]
        works, _ = self._run(items, ["li-rich", "anion redox"])
        title_only = set(config.CROSSREF_TITLE_ONLY_JOURNALS)
        # 无摘要的刊会放宽复核，所以只对"其余刊"断言「误命中被剔掉了」
        kept = {w["doi"] for w in works if w["journal"] not in title_only}
        self.assertEqual(kept, {"10.1002/adma.2"})
        normal = len(config.JOURNALS) - len(title_only)
        self.assertEqual(len(works), normal * 1 + len(title_only) * 2)

    def test_journal_name_comes_from_config_so_tier_bonus_still_resolves(self):
        # mock 让 15 本刊都返回同一批条目（真实场景里每本刊只会返回自己的论文），
        # 这里锁的是「刊名取自 config，而不是 Crossref 返回的 container-title」
        items = [self._item(doi="10.1002/adma.1", title="Li-rich layered oxide")]
        works, _ = self._run(items, ["li-rich"])
        self.assertTrue(all(w["journal"] in config.JOURNALS for w in works))
        self.assertIn("Advanced Materials", {w["journal"] for w in works})
        self.assertNotIn("Whatever Crossref Says", {w["journal"] for w in works})
        adma = [w for w in works if w["journal"] == "Advanced Materials"]
        self.assertEqual(adma[0]["issn"], config.JOURNALS["Advanced Materials"])

    def test_jats_xml_abstract_is_cleaned(self):
        items = [self._item(
            doi="10.1002/adma.1",
            title="A cathode design",
            abstract="<jats:p>An <jats:italic>anion redox</jats:italic> study</jats:p>",
        )]
        works, _ = self._run(items, ["anion redox"])
        self.assertTrue(works)
        self.assertNotIn("<", works[0]["abstract"])
        self.assertIn("anion redox", works[0]["abstract"])

    def test_every_journal_failing_raises(self):
        with self.assertRaises(RuntimeError):
            self._run([], ["li-rich"], status_code=500)

    # -- 「无摘要刊」放宽复核 -------------------------------------------
    def test_title_only_journals_relax_the_recheck_when_the_abstract_is_missing(self):
        """Joule / Nature Energy 在 Crossref 不带摘要 ⇒ 复核只能看标题 ⇒ 放宽。

        实测反例：Joule 近 30 天 2 条命中（全固态锂传输 / 储氢）剔除是对的，
        但同一本刊里标题写成 "Reversible anion storage in cathodes" 的富锂锰论文
        会被同一个判据杀掉 —— 而 OpenAlex 挂掉的那轮它没有任何兜底。
        """
        items = [
            self._item(doi="10.1016/j.joule.1", title="Reversible anion storage in cathodes")
        ]
        works, _ = self._run(items, ["li-rich", "anion redox"])
        # 名单内的刊全部留下（没摘要 ⇒ 判据残缺 ⇒ 交给 AI），其余刊照旧剔除
        self.assertEqual(
            {w["journal"] for w in works}, set(config.CROSSREF_TITLE_ONLY_JOURNALS)
        )

    def test_relaxation_requires_a_missing_abstract(self):
        """有摘要就说明复核是完整的，哪怕在放宽名单里也不能放行。"""
        items = [
            self._item(
                doi="10.1016/j.joule.2",
                title="Catalytic strategies for hydrogen release",
                abstract="<jats:p>Hydrogen storage and release.</jats:p>",
            )
        ]
        works, _ = self._run(items, ["anion redox"])
        self.assertEqual(works, [])

    # -- 限流重试 / 部分失败可见（缺陷 B）------------------------------
    def test_429_is_retried_so_a_whole_journal_is_not_lost(self):
        """实测：5 并发时 Crossref 回 429，整本 Angewandte 当轮消失（39 条 → 27 条）。

        重试之后这本刊照旧进来 —— 单刊失败不该表现为「本轮少了一本顶刊」。
        """
        issn = config.JOURNALS["Joule"]
        items = [self._item(
            doi="10.1016/j.joule.9",
            title="Li-rich cathode with anion redox",
            abstract="<jats:p>Anion redox in Li-rich cathodes.</jats:p>",
        )]
        works, calls, notes = self._run_scripted(
            {issn: [429, 429]}, items, ["li-rich", "anion redox"]
        )
        self.assertEqual(
            [c for c in calls if c[0] == issn], [(issn, 429), (issn, 429), (issn, 200)]
        )
        self.assertIn(issn, {w["issn"] for w in works})  # 这本刊没丢
        self.assertEqual(notes, [])  # 重试成功 ⇒ 不该吓用户

    def test_persistent_rate_limiting_is_reported_upwards(self):
        issn = config.JOURNALS["Joule"]
        works, calls, notes = self._run_scripted(
            {issn: [429] * 9}, [], ["li-rich", "anion redox"]
        )
        self.assertEqual(len([c for c in calls if c[0] == issn]), config.CROSSREF_RETRIES)
        self.assertEqual(len(notes), 1)
        self.assertIn(f"1/{len(config.JOURNALS)} 本刊查询失败", notes[0])
        self.assertIn("Joule", notes[0])
        self.assertEqual(works, [])

    def test_client_errors_are_not_retried(self):
        """404 重试多少次还是 404，白等只会拖慢整轮。"""
        issn = config.JOURNALS["Joule"]
        _, calls, _ = self._run_scripted({issn: [404] * 9}, [], ["li-rich", "anion redox"])
        self.assertEqual([c for c in calls if c[0] == issn], [(issn, 404)])

    def test_a_failing_journal_is_still_reported_when_the_rest_are_fine(self):
        """最阴险的情形：源是 ok，页头上只是一个偏小的篇数。"""
        items = [self._item(
            doi="10.1002/adma.88",
            title="Anion redox in Li-rich oxides",
            abstract="<jats:p>Anion redox in Li-rich oxides.</jats:p>",
        )]
        works, _, notes = self._run_scripted(
            {config.JOURNALS["Advanced Materials"]: [503] * 9},
            items, ["li-rich", "anion redox"],
        )
        # 其余 14 本刊照旧有结果，只是没了 AM
        self.assertTrue(works)
        self.assertNotIn("Advanced Materials", {w["journal"] for w in works})
        self.assertEqual(len(notes), 1)


class TestPartialSourceFailureVisibility(unittest.TestCase):
    """「源成功了但结果不完整」必须能进邮件页头，否则又是静默降级。"""

    def setUp(self):
        self._saved = dict(source_layer.ADAPTERS)
        self.addCleanup(self._restore)

    def _restore(self):
        source_layer.ADAPTERS.clear()
        source_layer.ADAPTERS.update(self._saved)

    def test_note_survives_into_the_report_and_the_notices(self):
        def _partial(terms, lookback_days, max_works=None, notes=None):
            if notes is not None:
                notes.append("有 1/15 本刊查询失败（Joule），这几本刊本轮的结果缺失。")
            return [source_base.make_work(doi="10.1/a", title="T", source="crossref")]

        source_layer.ADAPTERS["crossref"] = _partial
        result = source_layer.fetch_works(["anion redox"], 90, sources="crossref")

        report = result.reports[0]
        self.assertEqual(report.status, "ok")  # 源本身没失败
        self.assertIn("本刊查询失败", report.describe())
        self.assertIn("1/15", result.summary())
        notices = result.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("Crossref 有 1/15 本刊查询失败", notices[0])

    def test_adapters_without_the_notes_channel_are_untouched(self):
        """测试桩/旧适配器没有 notes 参数 ⇒ 不能因为探测而报 TypeError。"""
        source_layer.ADAPTERS["crossref"] = lambda *_a, **_k: [
            source_base.make_work(doi="10.1/a", title="T", source="crossref")
        ]
        result = source_layer.fetch_works(["anion redox"], 90, sources="crossref")
        self.assertEqual(result.reports[0].status, "ok")
        self.assertEqual(result.reports[0].notes, [])
        self.assertEqual(result.notices(), [])


class TestSemanticScholarAdapter(unittest.TestCase):
    """S2 的 OR 必须用竖线；期刊只能靠 DOI 前缀认。"""

    class _Resp:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    @staticmethod
    def _item(*, doi=None, title, venue="", abstract=None):
        item = {
            "title": title,
            "externalIds": {"DOI": doi} if doi else {},
            "venue": venue,
            "publicationDate": "2026-03-01",
            "publicationTypes": ["JournalArticle"],
        }
        if abstract:
            item["abstract"] = abstract
        return item

    def _run(self, items, terms):
        seen: dict = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return self._Resp({"total": len(items), "data": items})

        with patch.object(
            semantic_scholar_source.requests, "get", side_effect=fake_get
        ), patch.object(semantic_scholar_source.time, "sleep", lambda _s: None):
            works = semantic_scholar_source.fetch(terms, 90)
        return works, seen

    def test_or_uses_pipes_because_spaces_mean_and(self):
        """实测：同一组词用空格分隔返回 total=0 —— 不报错，只是安静地什么都没有。"""
        _, seen = self._run([], ["li-rich", "anion redox"])
        self.assertIn('"li-rich" | "anion redox"', seen["params"]["query"])
        self.assertNotIn(" AND ", seen["params"]["query"])

    def test_venue_filter_is_never_sent(self):
        """venue= 是模糊匹配，实测查到 47 条，而拉全量本地筛能得 108 条。"""
        _, seen = self._run([], ["li-rich"])
        self.assertNotIn("venue", seen["params"])

    def test_bulk_endpoint_and_open_ended_date_window(self):
        _, seen = self._run([], ["li-rich"])
        self.assertIn("/paper/search/bulk", seen["url"])
        self.assertTrue(str(seen["params"]["publicationDateOrYear"]).endswith(":"))
        self.assertIn("externalIds", seen["params"]["fields"])  # DOI 是唯一去重键

    def test_empty_recall_terms_raise(self):
        with self.assertRaises(RuntimeError) as ctx:
            semantic_scholar_source.fetch([], 90)
        self.assertIn("召回词", str(ctx.exception))

    def test_mislabelled_venue_is_corrected_by_the_doi_prefix(self):
        items = [self._item(
            doi="10.1002/adma.74958",
            title="Li-rich cathode with anion redox",
            venue="Advances in Materials",
        )]
        works, _ = self._run(items, ["li-rich", "anion redox"])
        self.assertEqual(works[0]["journal"], "Advanced Materials")
        self.assertEqual(works[0]["issn"], config.JOURNALS["Advanced Materials"])

    def test_unrecognised_journal_is_dropped_and_named_in_a_warning(self):
        """静默丢论文是不可接受的：被丢的是哪些刊必须写进日志。"""
        items = [self._item(
            doi="10.9999/unknown.1",
            title="Li-rich cathode with anion redox",
            venue="Some Other Journal",
        )]
        with self.assertLogs("src.sources.semantic_scholar", level="WARNING") as captured:
            works, _ = self._run(items, ["li-rich", "anion redox"])
        self.assertEqual(works, [])
        self.assertIn("Some Other Journal", "\n".join(captured.output))

    def test_records_without_doi_are_dropped(self):
        items = [self._item(
            doi=None, title="Li-rich cathode with anion redox", venue="Nature Energy"
        )]
        works, _ = self._run(items, ["li-rich", "anion redox"])
        self.assertEqual(works, [])

    def test_html_escaped_venue_is_unescaped(self):
        items = [self._item(
            doi="10.1039/d6ee01234a",
            title="Li-rich oxide with anion redox",
            venue="Energy &amp; Environmental Science",
        )]
        works, _ = self._run(items, ["li-rich", "anion redox"])
        self.assertEqual(works[0]["journal"], "Energy & Environmental Science")


class TestMailerSourceVisibility(unittest.TestCase):
    """数据源故障必须出现在**邮件**里 —— 只写日志等于没说。"""

    def test_source_line_and_failure_notice_reach_the_email(self):
        work = {
            "doi": "10.1/a", "title": "T", "journal": "Joule", "issn": "2542-4351",
            "pub_date": "2026-03-01", "ai_score": 70, "ai_takeaway": "x", "ai_reason": "y",
        }
        html_body, plain = mailer.build_html(
            [work], "2026-03-06", lookback_days=90, first_run=True,
            extra_meta="数据源：OpenAlex 8 篇 ｜ Crossref 失败（HTTP 500）",
            extra_notices=["⚠️ 本轮有数据源不可用（Crossref），结果由其余数据源合并而来"],
        )
        self.assertIn("数据源：OpenAlex 8 篇", html_body)
        self.assertIn("数据源不可用", html_body)
        self.assertIn("数据源不可用", plain)

    def test_heartbeat_email_also_carries_the_notice(self):
        """空结果邮件最容易被误读成「一切正常」，故障提示更要带上。"""
        html_body, plain = mailer.build_html(
            [], "2026-03-06", lookback_days=90, first_run=False,
            extra_notices=["⚠️ 本轮有数据源不可用（Crossref）"],
        )
        self.assertIn("数据源不可用", html_body)
        self.assertIn("数据源不可用", plain)

    def test_no_notice_means_no_warning_block(self):
        html_body, _ = mailer.build_html(
            [], "2026-03-06", lookback_days=14, first_run=False
        )
        self.assertNotIn("数据源不可用", html_body)


class TestSourceCliWiring(unittest.TestCase):
    """命令行与 --show-config 都要能看见数据源配置。"""

    def test_sources_flag_is_parsed_and_validated(self):
        args = main_module.build_parser().parse_args(["--sources", "crossref,s2"])
        self.assertEqual(
            source_layer.enabled_sources(args.sources), ["crossref", "semantic_scholar"]
        )

    def test_bad_sources_flag_fails_loudly(self):
        args = main_module.build_parser().parse_args(["--sources", "openaelx"])
        with self.assertRaises(ValueError):
            source_layer.enabled_sources(args.sources)

    def test_show_config_lists_data_sources(self):
        args = main_module.build_parser().parse_args(["--show-config"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main_module.show_config(args)

        self.assertEqual(code, 0)
        text = buffer.getvalue()
        # 换数据源是"能搜到什么"的第一层，必须离线可见
        self.assertIn("数据源", text)
        self.assertIn("OpenAlex", text)
        self.assertIn("Crossref", text)
        self.assertIn("Semantic Scholar", text)


class TestEmailItemLimit(unittest.TestCase):
    """邮件展示上限：首次预热 50 篇、之后 20 篇，命令行显式指定始终优先。

    首次窗口宽 6 倍，候选量也是同一量级（实测 50～110 篇），20 篇会把大半相关
    文献直接截掉；预热只来一次，所以单独放宽。这里锁定三个分支，免得以后
    改 ``--max-items`` 的默认值时把"首次放宽"悄悄弄丢。
    """

    CANDS = 60

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_file = config.PUSHED_FILE
        self._orig_topics = config.RESEARCH_TOPICS
        self._orig_outbox = main_module.OUTBOX_DIR
        config.PUSHED_FILE = os.path.join(self.tmp.name, "pushed.json")
        main_module.OUTBOX_DIR = os.path.join(self.tmp.name, "outbox")
        config.RESEARCH_TOPICS = [
            {"name": "富锂锰正极", "topic_query": "lithium rich cathode", "keywords": ["富锂锰"]}
        ]
        # 三个源都换成桩：这个用例只关心"展示几篇"，不该联网
        self._saved_adapters = dict(source_layer.ADAPTERS)
        self.addCleanup(self._restore)
        for name in source_layer.ALL_SOURCES:
            source_layer.ADAPTERS[name] = self._fake_source

    def _restore(self):
        config.PUSHED_FILE = self._orig_file
        config.RESEARCH_TOPICS = self._orig_topics
        main_module.OUTBOX_DIR = self._orig_outbox
        source_layer.ADAPTERS.clear()
        source_layer.ADAPTERS.update(self._saved_adapters)
        self.tmp.cleanup()

    def _fake_source(self, keywords, lookback_days, max_works=None, **_kw):
        return [
            {
                "doi": f"10.1/lit{index}",
                "openalex_id": f"W{index}",
                "title": f"Lithium-rich cathode paper {index}",
                "journal": "Joule",
                "issn": "2542-4351",
                "pub_date": "2026-09-01",
                "abstract": "abstract text",
                "doi_url": f"https://doi.org/10.1/lit{index}",
            }
            for index in range(self.CANDS)
        ]

    @staticmethod
    def _fake_eval(works, **_kw):
        return [
            dict(work, ai_score=70, ai_takeaway="解读", ai_reason="理由", ai_error=False)
            for work in works
        ], [], []

    @staticmethod
    def _never_send(*_args, **_kwargs):
        raise AssertionError("dry-run 不该发邮件")

    def _run_pipeline(self, argv: list[str], *, send, extra=()) -> int:
        """按给定命令行跑一轮（四个网络/IO 依赖全部换成桩）。

        ``send`` 是 ``send_mail`` 的 side_effect：dry-run 传 ``_never_send``，
        真发传一个收集器。``extra`` 用于追加 ``patch.object``（如临时改
        ``ATTACH_PDF_MAX_ITEMS``）。
        """
        args = main_module.build_parser().parse_args(argv + ["--topic", "富锂锰正极"])
        patchers = [
            patch.object(main_module, "validate_env", lambda **_kw: None),
            patch.object(main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works),
            patch.object(main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval),
            patch.object(main_module.mailer, "send_mail", side_effect=send),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in list(extra) + patchers:
                stack.enter_context(patcher)
            return main_module.run(args)

    def _latest(self, suffix: str) -> str:
        """outbox 里最新的某个后缀的文件名。

        必须先按后缀过滤：dry-run 现在会同时写出 ``.html`` 预览和 ``.pdf``
        附件，附件总是后写的，不过滤就会拿到二进制 PDF 当 HTML 解。
        """
        names = sorted(
            (name for name in os.listdir(main_module.OUTBOX_DIR) if name.endswith(suffix)),
            key=lambda name: os.path.getmtime(os.path.join(main_module.OUTBOX_DIR, name)),
        )
        self.assertTrue(names, f"dry-run 应该写出 {suffix} 文件")
        return names[-1]

    def _preview_html(self, argv: list[str] | None = None, extra=()) -> str:
        self.assertEqual(
            self._run_pipeline((argv or []) + ["--dry-run"], send=self._never_send, extra=extra), 0
        )
        with open(
            os.path.join(main_module.OUTBOX_DIR, self._latest(".html")), encoding="utf-8"
        ) as handle:
            return handle.read()

    def _attachment(self, argv: list[str] | None = None, extra=()) -> tuple[str, bytes]:
        self.assertEqual(
            self._run_pipeline((argv or []) + ["--dry-run"], send=self._never_send, extra=extra), 0
        )
        name = self._latest(".pdf")
        with open(os.path.join(main_module.OUTBOX_DIR, name), "rb") as handle:
            return name, handle.read()

    def _attachment_names(self) -> list[str]:
        return [name for name in os.listdir(main_module.OUTBOX_DIR) if name.endswith(".pdf")]

    def test_first_run_widens_the_limit_to_fifty(self):
        html_body = self._preview_html()
        # 60 篇候选 → 正文展示 50、剩下 10 篇不再“消失”：排进 PDF 附件一起发出
        self.assertIn("超出正文上限（50 篇）", html_body)
        self.assertIn("<b>10</b> 篇相关文献超出正文上限（50 篇）", html_body)
        self.assertIn("排进附件", html_body)
        self.assertIn("（共 10 篇）", html_body)
        self.assertNotIn("未在此展示", html_body)

        name, payload = self._attachment()
        self.assertTrue(name.endswith(".pdf"), name)
        self.assertTrue(name.startswith("富锂锰正极"), name)  # 附件名带主题名
        self.assertTrue(payload.startswith(b"%PDF-"), payload[:8])
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        # 附件里装的确实是正文没展示的那几篇（尾部第 59 篇在 50 篇正文之外）
        self.assertIn("10.1/lit59", _pdf_text(payload))

    def test_regular_run_keeps_the_twenty_item_limit(self):
        with patch.object(main_module.dedup, "is_first_run", return_value=False):
            html_body = self._preview_html()
        # 常规上限 20 篇，溢出 40 篇全部进附件
        self.assertIn("<b>40</b> 篇相关文献超出正文上限（20 篇）", html_body)
        self.assertIn("（共 40 篇）", html_body)

    def test_explicit_max_items_wins_in_both_cases(self):
        self.assertIn(
            "超出正文上限（5 篇）", self._preview_html(["--max-items", "5"])
        )
        with patch.object(main_module.dedup, "is_first_run", return_value=False):
            self.assertIn(
                "超出正文上限（8 篇）", self._preview_html(["--max-items", "8"])
            )

    def test_no_attachment_flag_restores_the_old_truncation(self):
        html_body = self._preview_html(["--no-attachment"])
        self.assertIn("<b>10</b> 篇相关文献因单封邮件上限（50 篇）未在此展示", html_body)
        self.assertNotIn("排进附件", html_body)
        self.assertEqual(self._attachment_names(), [])

    def test_overall_switch_can_turn_the_attachment_off(self):
        html_body = self._preview_html(extra=[patch.object(main_module, "OVERFLOW_ATTACHMENT", False)])
        self.assertNotIn("排进附件", html_body)
        self.assertEqual(self._attachment_names(), [])

    def test_cli_default_is_resolved_at_runtime(self):
        """--max-items 的默认值必须是 None，否则首次放宽无从判断。"""
        self.assertIsNone(main_module.build_parser().parse_args([]).max_items)
        self.assertEqual(config.MAX_EMAIL_ITEMS, 20)
        self.assertEqual(config.MAX_EMAIL_ITEMS_FIRST_RUN, 50)


    def test_first_run_email_subject_agrees_with_the_body(self):
        sent: list[tuple[str, str]] = []
        args = main_module.build_parser().parse_args(["--topic", "富锂锰正极"])
        with patch.object(main_module, "validate_env", lambda **_kw: None), patch.object(
            main_module.abstract_source, "enrich_abstracts", side_effect=lambda works: works
        ), patch.object(
            main_module.ai_matcher, "evaluate_works", side_effect=self._fake_eval
        ), patch.object(
            main_module.mailer,
            "send_mail",
            side_effect=lambda subject, html_body, plain_body, recipients=None, attachment=None: sent.append(
                (subject, html_body)
            ),
        ):
            self.assertEqual(main_module.run(args), 0)

        self.assertEqual(len(sent), 1)
        subject, html_body = sent[0]
        self.assertTrue(subject.endswith("· 50 篇"), subject)
        self.assertIn("超出正文上限（50 篇）", html_body)

    def test_attachment_items_are_marked_as_pushed(self):
        """排进附件 = 已经送到用户手里，必须记已读：

        不记的话下周会把它们原封不动地重新打分一遍，白烧 AI 额度。
        """
        sent: list[tuple[str, bytes] | None] = []

        def collect(subject, html_body, plain_body, recipients=None, attachment=None):
            sent.append(attachment)

        self.assertEqual(self._run_pipeline([], send=collect), 0)

        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(sent[0], "首次运行有 10 篇溢出，应该带附件")
        # 正文 50 篇 + 附件 10 篇 = 60 篇全部标记已读
        self.assertEqual(dedup.topic_counts(), {"富锂锰正极": 60})
        self.assertEqual(len(dedup.load_pushed("富锂锰正极")), 60)

    def test_attachment_cap_leaves_the_overflow_unmarked(self):
        """附件也有篇幅上限：装不下的宁可下轮重评，也不能冒充“已读”。"""
        sent: list[tuple[str, str]] = []

        def collect(subject, html_body, plain_body, recipients=None, attachment=None):
            sent.append((subject, html_body))

        self.assertEqual(
            self._run_pipeline([], send=collect, extra=[patch.object(main_module, "ATTACH_PDF_MAX_ITEMS", 3)]),
            0,
        )

        _, html_body = sent[0]
        self.assertIn("（共 3 篇）", html_body)
        self.assertIn("附件已收满", html_body)
        self.assertIn("另有 <b>7</b> 篇未装入附件", html_body)
        # 正文 50 + 附件 3 = 53；剩下 7 篇没被标记
        self.assertEqual(dedup.topic_counts(), {"富锂锰正极": 53})

    def test_attachment_failure_never_blocks_the_email(self):
        """PDF 生成挂了也必须把正文发出去 —— 正文才是主线。"""
        with patch.object(main_module.pdf_report, "build", side_effect=RuntimeError("PDF 挂了")):
            html_body = self._preview_html()

        self.assertNotIn("排进附件", html_body)
        self.assertIn("未在此展示", html_body)  # 退回旧的截断提示
        self.assertEqual(self._attachment_names(), [])

    def test_subject_count_follows_the_same_limit(self):
        """主题行写 20 篇而正文 50 篇，看起来就像邮件被截断了。"""
        works = [{"doi": f"10.1/x{index}"} for index in range(60)]
        self.assertEqual(
            mailer.build_subject(works, "2026-09-16", "富锂锰正极顶刊周报", max_items=50),
            "富锂锰正极顶刊周报 · 2026-09-16 · 50 篇",
        )
        # 不传就退回常规上限，保持旧调用方的行为
        self.assertEqual(
            mailer.build_subject(works, "2026-09-16").split(" · ")[-1],
            f"{config.MAX_EMAIL_ITEMS} 篇",
        )


class TestAuthorNames(unittest.TestCase):
    """``src/authors.py``：作者名的清洗、比较与那行显示文字。"""

    def setUp(self):
        from src import authors

        self.authors = authors

    def test_prints_first_and_corresponding(self):
        self.assertEqual(
            self.authors.author_line({"first_author": "Wei Zhang", "corresponding_author": "Yan Li"}),
            "Wei Zhang（一作） · Yan Li（通讯）",
        )

    def test_same_person_is_merged_into_one_label(self):
        """一作兼通讯只写一个名字，别让用户看到两个人。"""
        self.assertEqual(
            self.authors.author_line({"first_author": "J. Kim", "corresponding_author": "J Kim"}),
            "J. Kim（一作兼通讯）",
        )

    def test_only_first_author_when_corresponding_is_unknown(self):
        """Crossref / S2 只有一作：如实省略通讯，绝不拿末位作者冒充。"""
        self.assertEqual(self.authors.author_line({"first_author": "Wei Zhang"}), "Wei Zhang（一作）")

    def test_only_corresponding_when_first_is_unknown(self):
        self.assertEqual(self.authors.author_line({"corresponding_author": "Yan Li"}), "Yan Li（通讯）")

    def test_unknown_authors_produce_no_line_at_all(self):
        self.assertEqual(self.authors.author_line({}), "")
        self.assertEqual(self.authors.author_line({"first_author": "  ", "corresponding_author": None}), "")
        self.assertNotIn("未知", self.authors.author_line({}))

    def test_max_names_can_keep_only_the_first(self):
        work = {"first_author": "A B", "corresponding_author": "C D"}
        self.assertEqual(self.authors.author_line(work, max_names=1), "A B（一作）")

    def test_clean_name_collapses_whitespace(self):
        self.assertEqual(self.authors.clean_name("  Wei\n\tZhang  "), "Wei Zhang")
        self.assertEqual(self.authors.clean_name(None), "")

    def test_join_name_prefers_given_family_and_falls_back(self):
        self.assertEqual(self.authors.join_name("Wei", "Zhang"), "Wei Zhang")
        self.assertEqual(self.authors.join_name(None, None, "某研究所"), "某研究所")
        self.assertEqual(self.authors.join_name("", "", ""), "")

    def test_same_person_ignores_punctuation_and_case(self):
        self.assertTrue(self.authors.same_person("J. Kim", "j kim"))
        self.assertTrue(self.authors.same_person("李四", "李 四"))
        self.assertFalse(self.authors.same_person("J. Kim", "K. Jim"))
        self.assertFalse(self.authors.same_person("", ""))


class TestAuthorExtraction(unittest.TestCase):
    """各源把作者塞进统一结构，合并时缺失的补齐、先到的不被覆盖。"""

    AUTHORSHIPS = [
        {
            "author": {"display_name": "Wei Zhang"},
            "author_position": "first",
            "is_corresponding": False,
        },
        {
            "author": {"display_name": "Yan Li"},
            "author_position": "last",
            "is_corresponding": True,
        },
    ]

    def test_openalex_picks_first_and_corresponding(self):
        self.assertEqual(
            openalex_client.parse_authors(self.AUTHORSHIPS), ("Wei Zhang", "Yan Li")
        )

    def test_corresponding_ignores_author_position(self):
        """通讯作者跟署名位置无关 —— 只看 is_corresponding。"""
        authorships = [
            {"author": {"display_name": "A One"}, "author_position": "first", "is_corresponding": True},
            {"author": {"display_name": "Z Last"}, "author_position": "last", "is_corresponding": False},
        ]
        self.assertEqual(openalex_client.parse_authors(authorships), ("A One", "A One"))

    def test_missing_authorships_give_empty_strings(self):
        self.assertEqual(openalex_client.parse_authors(None), ("", ""))
        self.assertEqual(openalex_client.parse_authors([]), ("", ""))

    def test_author_name_falls_back_to_raw_name(self):
        self.assertEqual(openalex_client.author_name({"display_name": "Wei Zhang"}), "Wei Zhang")
        self.assertEqual(openalex_client.author_name({"raw_author_name": "W. Zhang"}), "W. Zhang")
        self.assertEqual(openalex_client.author_name(None), "")

    def test_parse_work_carries_both_author_fields(self):
        work = parse_work(
            {
                "doi": "10.1/x",
                "display_name": "T",
                "primary_location": None,
                "authorships": self.AUTHORSHIPS,
            }
        )
        assert work is not None
        self.assertEqual(work["first_author"], "Wei Zhang")
        self.assertEqual(work["corresponding_author"], "Yan Li")

    def test_crossref_takes_the_sequence_first_entry(self):
        item = {
            "author": [
                {"given": "Yan", "family": "Li", "sequence": "additional"},
                {"given": "Wei", "family": "Zhang", "sequence": "first"},
            ]
        }
        self.assertEqual(crossref_source._first_author(item), "Wei Zhang")

    def test_crossref_handles_organisation_authors(self):
        self.assertEqual(
            crossref_source._first_author({"author": [{"name": "某课题组"}]}), "某课题组"
        )
        self.assertEqual(crossref_source._first_author({}), "")

    def test_semantic_scholar_takes_the_first_name(self):
        self.assertEqual(
            semantic_scholar_source._first_author({"authors": [{"name": "W. Zhang"}, {"name": "Y. Li"}]}),
            "W. Zhang",
        )
        self.assertEqual(semantic_scholar_source._first_author({"authors": ["W. Zhang"]}), "W. Zhang")
        self.assertEqual(semantic_scholar_source._first_author({}), "")

    def test_make_work_cleans_author_names(self):
        work = source_base.make_work(
            doi="10.1/a",
            title="T",
            source="openalex",
            first_author="  Wei   Zhang ",
            corresponding_author="Yan Li",
        )
        assert work is not None
        self.assertEqual(work["first_author"], "Wei Zhang")
        self.assertEqual(work["corresponding_author"], "Yan Li")

    def test_make_work_without_authors_gives_empty_strings(self):
        work = source_base.make_work(doi="10.1/a", title="T", source="crossref")
        assert work is not None
        self.assertEqual(work["first_author"], "")
        self.assertEqual(work["corresponding_author"], "")

    def test_merge_keeps_openalex_authors_and_backfills_from_crossref(self):
        """OpenAlex 有通讯作者、Crossref 只有一作：合并结果两个字段都全。"""
        merged, _ = source_base.merge_works([
            ("openalex", [{
                "uid": "doi:10.1/a", "doi": "10.1/a", "title": "T",
                "first_author": "Wei Zhang", "corresponding_author": "Yan Li",
            }]),
            ("crossref", [{
                "uid": "doi:10.1/a", "doi": "10.1/a", "title": "T",
                "first_author": "W. Zhang", "corresponding_author": "",
            }]),
        ])
        self.assertEqual(merged[0]["first_author"], "Wei Zhang")  # 先到的不被覆盖
        self.assertEqual(merged[0]["corresponding_author"], "Yan Li")

    def test_merge_backfills_a_missing_first_author(self):
        merged, _ = source_base.merge_works([
            ("openalex", [{"uid": "doi:10.1/a", "doi": "10.1/a", "title": "T"}]),
            ("semantic_scholar", [{
                "uid": "doi:10.1/a", "doi": "10.1/a", "title": "T",
                "first_author": "W. Zhang", "corresponding_author": "",
            }]),
        ])
        self.assertEqual(merged[0]["first_author"], "W. Zhang")


class TestPdfReport(unittest.TestCase):
    """手写 PDF：GBK 安全化、按显示宽度硬换行、多页不切条目。"""

    def _work(self, index: int, **overrides) -> dict:
        work = {
            "doi": f"10.1/lit{index}",
            "title": f"Lithium-rich cathode paper {index}",
            "journal": "Joule",
            "pub_date": "2026-09-01",
            "final_score": 77,
            "ai_score": 70,
            "ai_takeaway": "解读文字",
            "ai_reason": "理由文字",
            "first_author": "Wei Zhang",
            "corresponding_author": "Yan Li",
        }
        work.update(overrides)
        return work

    def test_gbk_safe_drops_characters_the_font_cannot_show(self):
        from src import pdf_report

        # 预定义中文字体只覆盖 Adobe-GB1，emoji / 罕见字符显示成方块，宁可丢掉
        self.assertEqual(pdf_report.gbk_safe("富锂锰正极👍"), "富锂锰正极")
        # 各种“漂亮空白”（全角空格、不断行空格）统一成普通空格
        self.assertEqual(pdf_report.gbk_safe("Wei\u3000Zhang"), "Wei Zhang")
        self.assertEqual(pdf_report.gbk_safe("Wei\u00a0Zhang"), "Wei Zhang")

    def test_gbk_safe_keeps_gbk_characters_and_folds_the_rest(self):
        from src import pdf_report

        # 全角字母在 GBK 里有码位，原样保留；下标数字 GBK 没有，NFKC 折成 1/2/3
        self.assertEqual(pdf_report.gbk_safe("Ｌｉ₁₂₃"), "Ｌｉ123")
        self.assertEqual(pdf_report.gbk_safe("５０ mm"), "５０ mm")

    def test_wrap_never_exceeds_the_given_width(self):
        from src import pdf_report

        text = "富锂锰正极材料的电压衰减机理" * 6 + " and a long latin word here" * 3
        lines = pdf_report._wrap(text, 9.5, 200.0)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(pdf_report._text_width(line, 9.5), 200.0, repr(line))

    def test_wrap_respects_max_lines_and_ellipsises(self):
        from src import pdf_report

        lines = pdf_report._wrap("很长的解读" * 60, 9.5, 200.0, max_lines=3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("…"), lines[-1])

    def test_filename_is_readable_and_sanitised(self):
        from src import pdf_report

        self.assertEqual(
            pdf_report.filename_for("富锂锰正极", "2026-10-02", 12),
            "富锂锰正极-2026-10-02-附件12篇.pdf",
        )
        # 主题名里的路径分隔符不能带进文件名
        self.assertNotIn("/", pdf_report.filename_for("A/B:C", "2026-10-02", 1))
        self.assertNotIn(":", pdf_report.filename_for("A/B:C", "2026-10-02", 1))

    def test_build_returns_a_real_pdf(self):
        from src import pdf_report

        name, payload = pdf_report.build([self._work(1)], "2026-10-02", topic_name="富锂锰正极")
        self.assertTrue(name.endswith(".pdf"), name)
        self.assertTrue(payload.startswith(b"%PDF-"), payload[:8])
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        text = _pdf_text(payload)
        self.assertIn("10.1/lit1", text)
        self.assertIn("Wei Zhang（一作）", text)
        self.assertIn("Yan Li（通讯）", text)

    def test_build_paginates_without_splitting_items(self):
        from src import pdf_report

        works = [self._work(i) for i in range(1, 41)]
        name, payload = pdf_report.build(works, "2026-10-02", topic_name="富锂锰正极")
        text = payload.decode("latin-1")
        pages = text.count("/Type /Page ")
        self.assertGreater(pages, 1)
        self.assertIn(f"/Count {pages}".encode(), payload)
        # 条目连着编到 40，说明没有整条被丢掉
        body = _pdf_text(payload)
        self.assertIn("40. Joule", body)

    def test_build_survives_items_without_optional_fields(self):
        from src import pdf_report

        name, payload = pdf_report.build(
            [{"doi": "", "title": "", "journal": "", "pub_date": ""}], "2026-10-02"
        )
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertTrue(name.endswith(".pdf"))


class TestMailAttachment(unittest.TestCase):
    """附件与正文的 MIME 结构，以及 dry-run 预览落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list = []

        class FakeSMTP:
            def __init__(self, sink):
                self.sink = sink

            def login(self, user, password):
                self.sink.append(("login", user))

            def send_message(self, message):
                self.sink.append(("message", message))

            def quit(self):
                self.sink.append(("quit", None))

        self._fake_smtp = FakeSMTP

    def _patch_connect(self):
        return patch.object(mailer, "_connect", lambda: self._fake_smtp(self.sent))

    def _message(self):
        return [item[1] for item in self.sent if item[0] == "message"][0]

    def test_attachment_is_a_mixed_message_with_one_pdf_part(self):
        payload = b"%PDF-1.4\nfake\n%%EOF\n"
        with self._patch_connect():
            mailer.send_mail(
                "主题", "<p>hi</p>", "hi", recipients=["a@b.com"],
                attachment=("富锂锰正极-2026-10-02-附件3篇.pdf", payload),
            )
        message = self._message()
        self.assertEqual(message.get_content_type(), "multipart/mixed")
        kinds = [part.get_content_type() for part in message.walk()]
        self.assertIn("multipart/alternative", kinds)
        self.assertIn("application/pdf", kinds)
        pdf_part = [part for part in message.walk() if part.get_content_type() == "application/pdf"][0]
        self.assertEqual(pdf_part.get_payload(decode=True), payload)
        # 中文文件名走 RFC 2231；同时保留纯 ASCII 名字给老客户端
        content_types = pdf_part.get_all("Content-Type")
        self.assertEqual(len(content_types), 1, content_types)  # 不能发出两条同名头
        content_type = str(content_types[0])
        self.assertIn("name*=utf-8''", content_type)
        self.assertIn(
            "%E5%AF%8C%E9%94%82%E9%94%B0%E6%AD%A3%E6%9E%81-2026-10-02-%E9%99%84%E4%BB%B63%E7%AF%87.pdf",
            content_type,
        )
        self.assertIn('name="2026-10-02-3-.pdf"', content_type)  # ASCII 兜底
        dispositions = pdf_part.get_all("Content-Disposition")
        self.assertEqual(len(dispositions), 1, dispositions)
        disposition = str(dispositions[0])
        self.assertTrue(disposition.startswith("attachment;"), disposition)
        self.assertIn("filename*=utf-8''", disposition)
        self.assertIn("%E5%AF%8C", disposition)
        # 主题行仍然是编码过的中文，没有被附件挤掉
        self.assertTrue(message.get("Subject"), message.get("Subject"))

    def test_without_attachment_the_message_stays_alternative(self):
        """没附件时结构与以前完全一致，避免老客户端出现奇怪的空附件。"""
        with self._patch_connect():
            mailer.send_mail("主题", "<p>hi</p>", "hi", recipients=["a@b.com"])
        self.assertEqual(self._message().get_content_type(), "multipart/alternative")

    def test_ascii_filename_is_a_safe_fallback(self):
        self.assertEqual(mailer._ascii_filename("report.pdf"), "report.pdf")
        cleaned = mailer._ascii_filename("富锂锰正极-2026-10-02-附件3篇.pdf")
        self.assertTrue(cleaned.endswith(".pdf"), cleaned)
        self.assertTrue(all(ord(char) < 128 for char in cleaned), cleaned)
        self.assertEqual(mailer._ascii_filename("中文"), "literature-overflow.pdf")

    def test_save_preview_writes_html_and_pdf_side_by_side(self):
        outbox = os.path.join(self.tmp.name, "outbox")
        path = mailer.save_preview(
            "<p>hi</p>", "2026-10-02", outbox, suffix="-topic",
            attachment=("富锂锰正极-2026-10-02-附件3篇.pdf", b"%PDF-1.4 x"),
        )
        self.assertTrue(path.endswith(".html"), path)
        names = sorted(os.listdir(outbox))
        self.assertEqual(len(names), 2, names)
        self.assertTrue(any(name.endswith(".pdf") for name in names), names)

    def test_save_preview_without_attachment_writes_only_html(self):
        outbox = os.path.join(self.tmp.name, "outbox2")
        mailer.save_preview("<p>hi</p>", "2026-10-02", outbox)
        self.assertEqual([name for name in os.listdir(outbox) if name.endswith(".pdf")], [])

    def test_build_html_notices_switch_between_attachment_and_truncation(self):
        works = [
            {"doi": f"10.1/a{index}", "title": "T", "journal": "Joule", "pub_date": "2026-09-01"}
            for index in range(25)
        ]
        common = dict(
            run_date="2026-10-02", lookback_days=14, first_run=False, total_candidates=30,
            after_dedup=25, excluded=0, ai_failed=0, title="富锂锰正极顶刊周报", max_items=20,
        )
        with_attachment, plain_with = mailer.build_html(
            works, **common, attachment_name="a.pdf", attachment_count=7
        )
        self.assertIn("排进附件", with_attachment)
        self.assertIn("<b>5</b> 篇相关文献超出正文上限（20 篇）", with_attachment)
        self.assertIn("（共 7 篇）", with_attachment)
        self.assertIn("📎", plain_with)
        self.assertIn("a.pdf", plain_with)

        without_attachment, plain_without = mailer.build_html(works, **common)
        self.assertIn("<b>5</b> 篇相关文献因单封邮件上限（20 篇）未在此展示", without_attachment)
        self.assertNotIn("排进附件", without_attachment)
        self.assertNotIn("附件", without_attachment)
        self.assertNotIn("📎", plain_without)

    def test_build_html_says_nothing_about_attachment_without_overflow(self):
        """附件名传了但没溢出时，纯文本里不能出现“另 0 篇超出正文上限”。"""
        works = [{"doi": "10.1/a", "title": "T", "journal": "Joule", "pub_date": "2026-09-01"}]
        html_body, plain_body = mailer.build_html(
            works, "2026-10-02", lookback_days=14, first_run=False, total_candidates=1,
            after_dedup=1, excluded=0, ai_failed=0, title="富锂锰正极顶刊周报", max_items=20,
            attachment_name="a.pdf", attachment_count=0,
        )
        self.assertNotIn("📎", plain_body)
        self.assertNotIn("超出正文上限", html_body)

    def test_build_html_warns_when_the_attachment_hit_its_cap(self):
        works = [{"doi": "10.1/a", "title": "T", "journal": "Joule", "pub_date": "2026-09-01"}]
        html_body, plain_body = mailer.build_html(
            works, "2026-10-02", lookback_days=14, first_run=False, total_candidates=300,
            after_dedup=280, excluded=0, ai_failed=0, title="富锂锰正极顶刊周报", max_items=20,
            attachment_name="a.pdf", attachment_count=200, attachment_skipped=60,
        )
        self.assertIn("附件已收满", html_body)
        self.assertIn("另有 <b>60</b> 篇未装入附件", html_body)
        self.assertIn("被标记为已推送", html_body)
        self.assertIn("下一轮会重新评估", html_body)
        self.assertIn("60", plain_body)
        self.assertIn("📎 附件已收满", plain_body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
