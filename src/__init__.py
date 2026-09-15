"""顶刊文献自动推送机器人（研究方向由 config.py 配置）。

模块边界（只想改一件事时，只改对应的那一个文件）：

    config.py          所有常量：★研究方向（RESEARCH_FIELD/USER_KEYWORDS/
                       TOPIC_QUERY）、期刊 ISSN、时间窗、AI/邮件参数
    logger.py          统一日志（日志文件名使用北京时间）
    openalex_client.py 检索：给关键词，返回结构化文献列表
    abstract_source.py 摘要三级回退：OpenAlex → Crossref → Semantic Scholar
    ai_matcher.py      AI 相关性打分（任何 OpenAI 兼容端点，默认 DeepSeek）
    dedup.py           DOI 去重与状态持久化
    mailer.py          HTML 邮件渲染 + SMTP 发送
    main.py            主流程编排 + 命令行入口

运行方式：

    python -m src.main --dry-run --verbose   # 本地调试：只写 HTML，不发邮件
    python -m src.main                       # 正式运行
"""

__version__ = "1.0.0"
