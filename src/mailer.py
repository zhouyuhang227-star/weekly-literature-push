"""HTML 邮件渲染与发送。

相比最初骨架修正的关键点
------------------------
1. **HTML 转义**：标题/摘要里的 ``<``、``&``、``<i>``（化学式、上下标）
   会破坏 HTML 结构。所有动态字段一律 :func:`html.escape`。
2. **可点击 DOI**：渲染为 ``https://doi.org/<doi>`` 超链接。
3. **心跳邮件**：0 篇时不再静默不发，而是发一封"本周无新文献"，
   让用户能区分"没文献"和"程序挂了"。
4. **摘要暂缺标记**：摘要三级回退都失败时，在卡片上标注"依据：标题"，
   提示 AI 判断的可信度。
5. **SMTP SSL/STARTTLS 自动选择**：465 走 SMTP_SSL，587/25 走 STARTTLS。
   原骨架只支持 SSL，无法适配 163/Outlook 等要求 STARTTLS 的服务。
6. **补齐标准头**：``Date`` / ``Message-ID``，降低被判为垃圾邮件的概率。
7. **纯文本备选部分**：多部分 alternative，提升送达率。
8. **中文主题编码**：显式用 :class:`email.header.Header` 做 RFC 2047 编码，
   避免 compat32 policy 下序列化报 UnicodeEncodeError。
9. **HTML 注入防护**：即使某个字段混入标签也会被安全转义，不会破坏邮件结构。
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from . import ranking
from .config import (
    EMAIL_TITLE,
    MAX_EMAIL_ITEMS,
    SMTP_HOST,
    SMTP_PASS,
    SMTP_PORT,
    SMTP_USER,
    mail_recipients,
)

log = logging.getLogger(__name__)

# 内联样式，兼容 Gmail / Outlook / QQ 邮箱等客户端的 CSS 限制
_STYLE = {
    "body": "margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,'Segoe UI',"
    "'Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif;",
    "wrapper": "max-width:760px;margin:0 auto;padding:24px 16px;",
    "header": "padding:20px 24px;background:#1f2937;border-radius:12px 12px 0 0;color:#ffffff;",
    "header_title": "margin:0;font-size:20px;font-weight:600;letter-spacing:.3px;",
    "header_meta": "margin:8px 0 0;font-size:13px;color:#9ca3af;line-height:1.6;",
    "card": "background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;"
    "padding:18px 20px;margin:14px 0;",
    "meta": "font-size:12px;color:#6b7280;margin-bottom:8px;",
    "journal": "color:#111827;font-weight:600;",
    "score": "display:inline-block;padding:1px 8px;border-radius:999px;"
    "background:#ecfdf5;color:#047857;font-weight:600;margin-left:6px;",
    "title": "font-size:16px;font-weight:600;line-height:1.5;color:#111827;margin:0 0 10px;",
    "takeaway": "background:#f0f9ff;border-left:3px solid #0ea5e9;padding:10px 12px;"
    "border-radius:0 6px 6px 0;color:#0c4a6e;font-size:14px;line-height:1.7;margin:0 0 10px;",
    "reason": "font-size:13px;color:#4b5563;line-height:1.7;margin:0 0 10px;",
    "footer": "font-size:12px;color:#9ca3af;line-height:1.8;margin:0 0 12px;",
    "doi": "font-size:12px;color:#6b7280;padding-top:10px;border-top:1px dashed #e5e7eb;",
    "link": "color:#2563eb;text-decoration:none;",
    "tag": "display:inline-block;padding:1px 7px;border-radius:4px;background:#fef3c7;"
    "color:#92400e;font-size:11px;margin-right:6px;",
    "empty": "background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;"
    "padding:32px 24px;text-align:center;color:#6b7280;font-size:14px;line-height:1.8;",
    "notice": "background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;"
    "padding:12px 14px;font-size:13px;line-height:1.7;margin:14px 0;",
}


def _esc(value: object) -> str:
    """统一转义，None 安全。"""
    return html.escape("" if value is None else str(value), quote=True)


def _render_card(work: dict) -> str:
    doi = work.get("doi") or ""
    doi_url = work.get("doi_url") or (f"https://doi.org/{doi}" if doi else "")

    # 最终分 = AI 相关性分 + 期刊档次加成（缺失排序字段时退化成 AI 分，测试友好）
    ai_score = work.get("ai_score")
    bonus = int(work.get("journal_bonus") or 0)
    tier = str(work.get("journal_tier") or "").strip()
    if work.get("final_score") is None:
        final_score = int(ai_score or 0) + bonus
    else:
        final_score = int(work["final_score"])

    tags: list[str] = []
    if bonus > 0:
        tags.append(
            f'<span style="{_STYLE["tag"]}">{_esc(tier)} +{bonus}'
            f'（AI {_esc(ai_score)}）</span>'
        )
    if not (work.get("abstract") or "").strip():
        tags.append(f'<span style="{_STYLE["tag"]}">依据：标题（摘要缺失）</span>')
    if work.get("ai_error"):
        tags.append(f'<span style="{_STYLE["tag"]}">AI 打分失败</span>')
    tag_html = "".join(tags)

    takeaway = (work.get("ai_takeaway") or "").strip()
    takeaway_html = (
        f'<div style="{_STYLE["takeaway"]}">{_esc(takeaway)}</div>' if takeaway else ""
    )

    doi_html = (
        f'DOI：<a style="{_STYLE["link"]}" href="{_esc(doi_url)}">{_esc(doi)}</a>'
        if doi
        else "DOI：<i>缺失</i>"
    )

    return f"""\
<div style="{_STYLE['card']}">
  <div style="{_STYLE['meta']}">
    <span style="{_STYLE['journal']}">{_esc(work.get('journal'))}</span>
    &nbsp;·&nbsp;{_esc(work.get('pub_date') or '日期未知')}
    <span style="{_STYLE['score']}">{'最终' if bonus > 0 else 'AI'} {_esc(final_score)} 分</span>
    &nbsp;{tag_html}
  </div>
  <div style="{_STYLE['title']}">{_esc(work.get('title'))}</div>
  {takeaway_html}
  <div style="{_STYLE['reason']}">判断理由：{_esc(work.get('ai_reason') or '无')}</div>
  <div style="{_STYLE['doi']}">{doi_html}</div>
</div>"""


def build_html(
    works: list[dict],
    run_date: str,
    *,
    lookback_days: int,
    first_run: bool,
    total_candidates: int = 0,
    after_dedup: int = 0,
    ai_failed: int = 0,
    title: str | None = None,
    extra_meta: str = "",
    max_items: int | None = None,
) -> tuple[str, str]:
    """渲染邮件正文。

    :param title: 邮件标题前缀；为空时用 ``config.EMAIL_TITLE``
                  （多主题调研时每个主题传自己的标题）。
    :param extra_meta: 追加到头部元信息行末尾的说明（已转义前的纯文本）。
    :param max_items: 单封最多展示篇数；默认 ``config.MAX_EMAIL_ITEMS``。
    :return: ``(html, 纯文本备选)``
    """
    email_title = title or EMAIL_TITLE
    limit = MAX_EMAIL_ITEMS if max_items is None else max(1, int(max_items))
    scope = "首次预热" if first_run else "常规滚动"
    meta_line = (
        f"检索窗口：近 {lookback_days} 天（{scope}） &nbsp;·&nbsp; "
        f"候选 {total_candidates} 篇 &nbsp;·&nbsp; 去重后 {after_dedup} 篇"
    )
    if extra_meta:
        meta_line = f"{meta_line} &nbsp;·&nbsp; {_esc(extra_meta)}"

    if not works:
        body = f"""\
<div style="{_STYLE['empty']}">
  <div style="font-size:32px;margin-bottom:12px;">📭</div>
  <div style="font-size:16px;font-weight:600;color:#374151;margin-bottom:8px;">本周没有新的相关文献</div>
  <div>程序已正常运行，只是近 {_esc(lookback_days)} 天没有符合条件的新论文。</div>
  <div style="margin-top:12px;font-size:12px;color:#9ca3af;">{meta_line}</div>
</div>"""
        plain = (
            f"{email_title} · {run_date}\n"
            f"本周没有新的相关文献。\n"
            f"检索窗口：近 {lookback_days} 天（{scope}）\n"
            f"候选 {total_candidates} 篇，去重后 {after_dedup} 篇。\n"
        )
        return _wrap(body, run_date, meta_line, email_title), plain

    shown = works[:limit]
    overflow = len(works) - len(shown)

    cards = "\n".join(_render_card(work) for work in shown)

    notices: list[str] = []
    if overflow > 0:
        notices.append(
            f"另有 <b>{overflow}</b> 篇相关文献因单封邮件上限（{limit} 篇）未在此展示，"
            "已按<b>最终分（AI 相关性分 + 期刊档次加成）</b>降序截断。"
        )
    if ai_failed:
        notices.append(f"有 <b>{ai_failed}</b> 篇文献 AI 打分失败，本次未纳入统计（详见运行日志）。")
    if first_run:
        notices.append(
            "这是<b>首次运行</b>，使用了更宽的预热窗口；"
            "未被展示的文献仍会保留在后续窗口中被重新评估。"
        )
    notice_html = "".join(f'<div style="{_STYLE["notice"]}">{n}</div>' for n in notices)

    body = f'{notice_html}\n{cards}'

    plain_lines = [f"{email_title} · {run_date}（{len(shown)} 篇）", ""]
    for index, work in enumerate(shown, start=1):
        plain_lines += [
            f"{index}. {work.get('title')}",
            f"   [{work.get('journal')}] {work.get('pub_date')} · {ranking.breakdown(work)} 分",
        ]
        if work.get("ai_takeaway"):
            plain_lines.append(f"   解读：{work['ai_takeaway']}")
        if work.get("ai_reason"):
            plain_lines.append(f"   理由：{work['ai_reason']}")
        plain_lines.append(f"   DOI：https://doi.org/{work.get('doi')}")
        plain_lines.append("")
    plain_lines += [f"检索窗口：近 {lookback_days} 天（{scope}），候选 {total_candidates} 篇。"]
    plain = "\n".join(plain_lines)

    return _wrap(body, run_date, meta_line, email_title), plain


def _wrap(body: str, run_date: str, meta_line: str, title: str | None = None) -> str:
    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="{_STYLE['body']}">
  <div style="{_STYLE['wrapper']}">
    <div style="{_STYLE['header']}">
      <p style="{_STYLE['header_title']}">{_esc(title or EMAIL_TITLE)} · {_esc(run_date)}</p>
      <p style="{_STYLE['header_meta']}">{meta_line}</p>
    </div>
    {body}
    <p style="{_STYLE['footer']}">
      本邮件由 GitHub Actions 自动生成。期刊范围与关键词可在仓库 <code>src/config.py</code> 中调整。<br>
      排序规则：最终分 = AI 相关性分 + 期刊档次加成（正刊/大子刊/Joule/小子刊/JACS/Angew/AM）。<br>
      邮件正文中所有字段均已做 HTML 转义，DOI 链接指向 doi.org 官方解析。
    </p>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _connect() -> smtplib.SMTP:
    """按端口自动选择 SSL 或 STARTTLS。

    - 465：隐式 SSL（``smtplib.SMTP_SSL``）
    - 587 / 25 / 其它：明文连接后 ``STARTTLS``
    """
    context = ssl.create_default_context()
    host = SMTP_HOST.strip()

    if SMTP_PORT == 465:
        log.info("SMTP 连接：%s:%s（SSL）", host, SMTP_PORT)
        return smtplib.SMTP_SSL(host, SMTP_PORT, timeout=30, context=context)

    log.info("SMTP 连接：%s:%s（STARTTLS）", host, SMTP_PORT)
    server = smtplib.SMTP(host, SMTP_PORT, timeout=30)
    try:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    except smtplib.SMTPException:
        log.warning("服务器不支持 STARTTLS，将使用明文连接（不推荐）")
    return server


def send_mail(subject: str, html_body: str, plain_body: str, recipients: list[str] | None = None) -> None:
    """发送 multipart/alternative 邮件。失败时抛出异常，由调用方决定是否回写状态。"""
    recipients = recipients or mail_recipients()
    if not recipients:
        raise RuntimeError("收件人列表为空（MAIL_TO 未配置）")

    message = MIMEMultipart("alternative")
    # 中文主题必须显式做 RFC 2047 编码
    message["Subject"] = Header(subject, "utf-8").encode()
    message["From"] = SMTP_USER
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="literature-bot")

    message.attach(MIMEText(plain_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    server = _connect()
    try:
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass

    log.info("邮件已发送至 %s（主题：%s）", ", ".join(recipients), subject)


def build_subject(works: list[dict], run_date: str, title: str | None = None) -> str:
    """邮件主题。

    ``main.py`` 与 ``send()`` 都要用它，之前两处各写一份已出现过不一致，
    现在统一到这里，标题前缀默认由 ``config.EMAIL_TITLE`` 派生；
    多主题调研时由调用方传入该主题自己的标题。
    """
    prefix = title or EMAIL_TITLE
    if works:
        count = min(len(works), MAX_EMAIL_ITEMS)
        return f"{prefix} · {run_date} · {count} 篇"
    return f"{prefix} · {run_date} · 本周无新文献"


def send(
    works: list[dict],
    run_date: str,
    *,
    lookback_days: int,
    first_run: bool,
    total_candidates: int = 0,
    after_dedup: int = 0,
    ai_failed: int = 0,
    recipients: list[str] | None = None,
    title: str | None = None,
) -> dict:
    """渲染并发送。返回统计信息供日志记录。

    这是 ``build_html`` + ``send_mail`` 的便捷封装；
    ``main.py`` 因为需要先渲染再决定 dry-run，所以直接调用那两个函数。
    """
    html_body, plain_body = build_html(
        works,
        run_date,
        lookback_days=lookback_days,
        first_run=first_run,
        total_candidates=total_candidates,
        after_dedup=after_dedup,
        ai_failed=ai_failed,
        title=title,
    )

    subject = build_subject(works, run_date, title)
    send_mail(subject, html_body, plain_body, recipients=recipients)
    return {"subject": subject, "count": min(len(works), MAX_EMAIL_ITEMS), "html": html_body}


def save_preview(html_body: str, run_date: str, outbox_dir: str, suffix: str = "") -> str:
    """``--dry-run`` 模式：把邮件 HTML 写到本地文件，供浏览器预览。

    ``suffix`` 用于多主题时区分每个主题的预览（如 ``-无负极钠离子电池``）。
    """
    import os

    os.makedirs(outbox_dir, exist_ok=True)
    path = os.path.join(outbox_dir, f"{run_date}{suffix}.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html_body)
    log.info("[dry-run] 邮件预览已写入：%s", path)
    return path
