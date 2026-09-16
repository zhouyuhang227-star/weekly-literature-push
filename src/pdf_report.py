"""把"超出邮件正文上限"的文献渲染成一份 PDF 附件。

为什么要**手写** PDF
--------------------
``requirements.txt`` 里只有 ``requests`` / ``tzdata`` 两个依赖。生态里能写中文
PDF 的库（reportlab / fpdf2 / weasyprint / pypdf）都是重量级依赖（体积、编译、
许可证、还得跟着 Python 版本升级），为了一封周报邮件不值得。PDF 的文本层其实
就是一个"内容流 + 交叉引用表"，手写 200 行足够。

中文怎么办：**非嵌入的预定义 CJK 字体**
-------------------------------------
PDF 规范里有一条专给中文的路径（也是早年 Office / WPS 导出 PDF 用的）：

* 字体写 ``/BaseFont /STSong-Light``（简体中文标准字体，Adobe-GB1 字符集），
  编码写 ``/Encoding /UniGB-UCS2-H``（UTF-16BE 二字节码）；
* **阅读器自带字体映射**，所以我们不嵌字体 ⇒ 附件只有几十 KB，
  也不涉及任何字体的版权；
* 代价：文字必须落在 **Adobe-GB1（≈GBK）字符集**里。``gbk_safe()`` 负责把
  下标数字（Li₁.₂ 这种）、emoji、罗马数字等降级成 GBK 里的等价写法或丢弃 ——
  宁可少一个字，也不要输出 ``.notdef`` 方块。

兼容性：Adobe Reader / Chrome / Edge / Firefox(pdf.js) / macOS 预览 / iOS 文件 /
手机 X5 内核（PDFium）都能正常显示。极少数精简阅读器可能不认预定义 CJK 字体，
那样标题与英文仍会显示、中文为空 —— 这是已知取舍，要彻底解决必须改嵌字体
（约 5-8 MB，且需从系统里取字体文件），不划算。

排版
----
正文用**左侧绝对定位**（每行一条 ``Tm`` 文本矩阵，不是 PDF 的"连续文本流"），
换行由我们自己按字宽算：中日韩字符按 1.0 em、其余按 0.5 em。这样：
* 不依赖阅读器的自动换行（否则中文会因为"没有空格"整段溢出）；
* 长条目会自动分页，且**尽量不在条目中间断页**。

PDF 里放不下的文字**不会**被截断成乱码：每种样式都有行数上限（标题 3 行、
解读 4 行、理由 2 行，见 ``_MAX_LINES``），超出部分以 ``…`` 收尾。
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
import zlib
from functools import lru_cache

from . import authors, config, ranking

log = logging.getLogger(__name__)

#: A4，单位 pt（1pt = 1/72 英寸）
PAGE_W = 595.28
PAGE_H = 841.89
MARGIN_X = 52.0
MARGIN_TOP = 54.0
MARGIN_BOTTOM = 50.0
#: 字体资源名（内容流里引用），见 _assemble()
FONT_RES = "F1"

# ---------------------------------------------------------------- 文字清洗

#: 无信息量、直接删掉的控制/零宽字符
_DROP = frozenset("\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u0000\u0001\u0002")
#: 各种"空白"统一成半角空格（含全角空格 U+3000）
_SPACES = frozenset(
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


@lru_cache(maxsize=8192)
def _gbk_ok(char: str) -> bool:
    """这个字符能不能编码进 Adobe-GB1（用 GBK 近似判断）。"""
    try:
        char.encode("gbk")
    except UnicodeEncodeError:
        return False
    return True


def gbk_safe(text: object) -> str:
    """把任意文本降级成 Adobe-GB1 里有的字符（PDF 里能正常显示的字符）。

    处理顺序：删无信息字符 → 空白归一 → **本来就在 GBK 里的原样保留**（中文、
    希腊字母、全角字母都算，GBK 里有它们的码位）→ 其余试 NFKC 归一
    （``₁`` → ``1``、``㎜`` → ``mm``）→ 都对不上就丢掉。

    ``_em()`` 按 Unicode 的 East Asian Width 算宽度，全角字母恰好被当成 1.0 em，
    与浏览器/阅读器的实际显示宽度一致，所以不需要额外把全角改半角。
    """
    out: list[str] = []
    for char in str(text if text is not None else ""):
        if char == "\n":
            out.append("\n")
        elif char == "\t":
            out.append(" ")
        elif char in _DROP:
            continue
        elif char in _SPACES:
            out.append(" ")
        elif char.isascii() or _gbk_ok(char):
            out.append(char)
        else:
            folded = unicodedata.normalize("NFKC", char)
            if folded and folded != char and all(c.isascii() or _gbk_ok(c) for c in folded):
                out.append(folded)
            # 否则丢弃：少一个字，也好过显示成方块
    return "".join(out)


# ---------------------------------------------------------------- 排版计算


@lru_cache(maxsize=8192)
def _em(char: str) -> float:
    """字符宽度（单位：em）。中日韩用 1.0，其余用 0.5。"""
    return 1.0 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 0.5


def _text_width(text: str, size: float) -> float:
    return sum(_em(char) for char in text) * size


def _tokens(paragraph: str) -> list[str]:
    """切词：CJK/全角字符各成一个可断单元，连续的西文串整体不断开。"""
    tokens: list[str] = []
    buffer: list[str] = []
    for char in paragraph:
        if char == " ":
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            tokens.append(" ")
        elif _em(char) >= 1.0:
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            tokens.append(char)
        else:
            buffer.append(char)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _wrap(text: object, size: float, max_width: float, max_lines: int | None = None) -> list[str]:
    """按字宽硬折行（不靠阅读器自动换行）。``max_lines`` 超出时补 ``…``。"""
    if not text:
        return []
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        current = ""
        width = 0.0
        for token in _tokens(paragraph):
            token_width = _text_width(token, size)
            if token == " ":
                if current:
                    current += " "
                    width += token_width
                continue
            if width + token_width > max_width and current:
                lines.append(current.rstrip())
                current, width = "", 0.0
            if token_width > max_width:
                # 单个超长单元（超长英文单词 / 没空格的 URL）只能硬断
                for char in token:
                    char_width = _em(char) * size
                    if width + char_width > max_width and current:
                        lines.append(current.rstrip())
                        current, width = "", 0.0
                    current += char
                    width += char_width
                continue
            current += token
            width += token_width
        lines.append(current.rstrip())

    while lines and not lines[-1].strip():
        lines.pop()
    if max_lines is not None and len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1].rstrip()
        kept[-1] = (last[:-1] if len(last) > 1 else "") + "…"
        lines = kept
    return lines


# ---------------------------------------------------------------- PDF 底层

#: PDF 的名字对象里不能出现的字符：分隔符与空白
_BAD_PDF_NAME = re.compile(r"[\s()<>\[\]{}/%#]+")


def _pdf_name(text: str, fallback: str = "STSongLight") -> str:
    """清理 ``/Name`` 里不合法的字符（连字符是合法的，要留着）。"""
    cleaned = _BAD_PDF_NAME.sub("", str(text or "")).strip()
    return cleaned or fallback


class _Page:
    """一页的内容流（只用到文本与矩形两个操作符）。"""

    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(self, x: float, y: float, size: float, content: object, gray: float = 0.1) -> None:
        clean = gbk_safe(content)
        if not clean.strip():
            return
        payload = clean.encode("utf-16-be").hex().upper()
        self.ops.append(
            f"BT {gray:.3f} {gray:.3f} {gray:.3f} rg /{FONT_RES} {size:.2f} Tf "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm <{payload}> Tj ET"
        )

    def rect(self, x: float, y: float, width: float, height: float, gray: float = 0.85) -> None:
        self.ops.append(
            f"{gray:.3f} {gray:.3f} {gray:.3f} rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f"
        )

    def hline(self, x0: float, x1: float, y: float, gray: float = 0.82, thickness: float = 0.6) -> None:
        self.rect(x0, y, x1 - x0, thickness, gray)

    def stream(self) -> bytes:
        body = "\n".join(self.ops).encode("latin-1", "replace")
        return zlib.compress(body, 6)


# 样式：字号 / 灰度 / 行高 / 左缩进 / 段前 / 段后
_BLOCK_STYLES: dict[str, dict[str, float]] = {
    "h1": {"size": 15.0, "gray": 0.10, "leading": 20.0, "indent": 0.0, "before": 0.0, "after": 3.0},
    "h2": {"size": 9.0, "gray": 0.42, "leading": 13.0, "indent": 0.0, "before": 0.0, "after": 0.5},
    "h3": {"size": 8.0, "gray": 0.55, "leading": 11.5, "indent": 0.0, "before": 0.0, "after": 1.0},
    "item_meta": {"size": 8.5, "gray": 0.42, "leading": 12.5, "indent": 0.0, "before": 7.0, "after": 1.5},
    "title": {"size": 10.5, "gray": 0.08, "leading": 15.0, "indent": 0.0, "before": 0.5, "after": 1.0},
    "author": {"size": 8.5, "gray": 0.45, "leading": 12.5, "indent": 0.0, "before": 0.0, "after": 1.0},
    "takeaway": {"size": 9.5, "gray": 0.16, "leading": 14.0, "indent": 12.0, "before": 1.5, "after": 1.0},
    "reason": {"size": 9.0, "gray": 0.38, "leading": 13.0, "indent": 12.0, "before": 0.0, "after": 0.5},
    "doi": {"size": 8.5, "gray": 0.30, "leading": 12.0, "indent": 0.0, "before": 1.0, "after": 0.0},
}

#: 每种样式最多显示几行（超出补省略号）
_MAX_LINES: dict[str, int] = {
    "h1": 2,
    "h2": 1,
    "h3": 2,
    "item_meta": 2,
    "title": 3,
    "author": 1,
    "takeaway": 4,
    "reason": 2,
    "doi": 1,
}


class _Layout:
    """按块排版，块放不下就开新页。"""

    def __init__(self) -> None:
        self.pages: list[_Page] = []
        self.page = self._new_page()
        self.y = PAGE_H - MARGIN_TOP

    def _new_page(self) -> _Page:
        page = _Page()
        self.pages.append(page)
        self.y = PAGE_H - MARGIN_TOP
        return page

    def ensure(self, height: float) -> None:
        if self.y - height < MARGIN_BOTTOM:
            self.page = self._new_page()

    def blocks(self, blocks: list[tuple[str, object]], content_w: float) -> None:
        """先量高度（整块不跨页），再逐行输出。"""
        prepared: list[tuple[dict[str, float], list[str]]] = []
        height = 0.0
        for style, text in blocks:
            spec = _BLOCK_STYLES[style]
            lines = _wrap(text, spec["size"], content_w - spec["indent"], _MAX_LINES.get(style))
            if not lines:
                continue
            prepared.append((spec, lines))
            height += spec["before"] + spec["after"] + spec["leading"] * len(lines)
        if not prepared:
            return
        self.ensure(height)
        for spec, lines in prepared:
            self.y -= spec["before"]
            for line in lines:
                self.y -= spec["leading"]
                self.page.text(MARGIN_X + spec["indent"], self.y, spec["size"], line, spec["gray"])
            self.y -= spec["after"]

    def rule(self) -> None:
        self.ensure(_BLOCK_STYLES["h2"]["leading"] + 8)
        self.y -= 5.0
        self.page.hline(MARGIN_X, PAGE_W - MARGIN_X, self.y, 0.80, 0.7)
        self.y -= 6.0


# ---------------------------------------------------------------- 内容拼装


def _item_blocks(work: dict, index: int) -> list[tuple[str, object]]:
    """一条文献 = 与邮件卡片同构的一组文本块。"""
    journal = str(work.get("journal") or "期刊未知").strip() or "期刊未知"
    pub_date = str(work.get("pub_date") or "日期未知").strip() or "日期未知"
    final = int(work.get("final_score") or 0)
    detail = ranking.breakdown(work)

    blocks: list[tuple[str, object]] = [
        ("item_meta", f"{index}. {journal} · {pub_date} · 最终 {final} 分（{detail}）"),
        ("title", work.get("title") or "(无标题)"),
    ]
    # 保底标签：附件里也可能出现被保底的论文（本周无负极论文特别多时），
    # 得能一眼看出它为什么排在前面。
    # 不用 🎯 之类的 emoji：PDF 用的是 Adobe-GB1 字符集，emoji 编不进去，
    # gbk_safe() 会把它们悄悄丢掉（不报错，只是没了）。
    if work.get("force_keep") or str(work.get("keep_reason") or "").strip():
        blocks.append(
            ("item_meta", f"【硬保底】{str(work.get('keep_reason') or '命中保底规则').strip()}")
        )
    line = authors.author_line(work)
    if line:
        blocks.append(("author", f"作者：{line}"))

    takeaway = str(work.get("ai_takeaway") or "").strip()
    if takeaway:
        blocks.append(("takeaway", f"解读：{takeaway}"))

    reason = str(work.get("ai_reason") or "").strip()
    if reason:
        blocks.append(("reason", f"理由：{reason}"))

    doi = str(work.get("doi") or "").strip()
    url = str(work.get("doi_url") or "").strip()
    blocks.append(("doi", f"DOI：{doi or '缺失'}" + (f"　{url}" if url and url != doi else "")))
    return blocks


def _header_blocks(
    *,
    topic_name: str,
    run_date: str,
    lookback_days: int,
    first_run: bool,
    shown_count: int,
    total_selected: int,
    count: int,
    skipped: int,
    sources: str,
) -> list[tuple[str, object]]:
    scope = "首次运行预热" if first_run else "常规滚动"
    window = f"近 {lookback_days} 天" if lookback_days else "未指定"
    blocks: list[tuple[str, object]] = [
        ("h1", f"{topic_name} · 超出邮件正文上限的文献"),
        ("h2", f"生成日期：{run_date}　·　检索窗口：{window}（{scope}）"),
        (
            "h2",
            f"邮件正文已展示 {shown_count} 篇（本轮入选 {total_selected} 篇）"
            f"　·　本附件收录 {count} 篇，按最终分降序",
        ),
    ]
    if skipped > 0:
        blocks.append(
            ("h3", f"另有 {skipped} 篇未收录（超出附件篇幅上限）：它们不会被标记为已推送，下轮会重新评估")
        )
    blocks.append(("h3", "排序：最终分 = AI 相关性分 + 期刊档次加成 + 内容加分；加成只影响顺序，不影响入选"))
    if sources:
        blocks.append(("h3", f"数据源：{sources}"))
    return blocks


def _safe_filename(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(text or "")).strip(" -.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:limit] or "文献"


def filename_for(topic_name: str, run_date: str, count: int) -> str:
    """附件文件名：``主题-日期-附件N篇.pdf``。"""
    return f"{_safe_filename(topic_name)}-{run_date}-附件{int(count)}篇.pdf"


def _assemble(pages: list[_Page], *, font: str, encoding: str, ordering: str, title: str) -> bytes:
    """拼出完整的 PDF 文件字节（含 xref 交叉引用表）。"""
    font_name = _pdf_name(font)
    catalog, pages_obj, font_obj, cid_obj, desc_obj, info_obj = 1, 2, 3, 4, 5, 6
    page_nums = [7 + 2 * index for index in range(len(pages))]

    objects: list[bytes] = []
    kids = " ".join(f"{num} 0 R" for num in page_nums)
    objects.append(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("ascii"))
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    )
    objects.append(
        (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{font_name} "
            f"/Encoding /{_pdf_name(encoding, 'UniGB-UCS2-H')} "
            f"/DescendantFonts [{cid_obj} 0 R] >>"
        ).encode("ascii")
    )
    # /DW 1000：中日韩字符默认整字宽（1.0 em），与 _em() 的估算一致。
    # /W [1 95 500]：ASCII（UniGB-UCS2-H 里 U+0020..U+007E → CID 1..95）按半宽
    # 计，正好对上 _em() 给西文算的 0.5 em。不给这一项，西文会按整字宽排版，
    # 长英文标题就会顶出右边距。
    objects.append(
        (
            f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{font_name} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering ({ordering}) "
            f"/Supplement 4 >> /FontDescriptor {desc_obj} 0 R /DW 1000 /W [1 95 500] >>"
        ).encode("ascii")
    )
    objects.append(
        (
            f"<< /Type /FontDescriptor /FontName /{font_name} /Flags 4 "
            f"/FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 /Descent -120 "
            f"/CapHeight 700 /StemV 93 >>"
        ).encode("ascii")
    )
    info = (
        f"<< /Title <{gbk_safe(title).encode('utf-16-be').hex().upper()}> "
        f"/Producer (weekly-literature-push) /Creator (weekly-literature-push) "
        f"/CreationDate (D:{_pdf_date()}) >>"
    )
    objects.append(info.encode("ascii"))

    for num, page in zip(page_nums, pages):
        content = page.stream()
        objects.append(
            (
                f"<< /Type /Page /Parent {pages_obj} 0 R "
                f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                f"/Resources << /Font << /{FONT_RES} {font_obj} 0 R >> >> "
                f"/Contents {num + 1} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(content)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
            + content
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info_obj} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _pdf_date() -> str:
    """PDF 的时间戳格式：``YYYYMMDDHHmmSS+08'00'``。"""
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "+08'00'"


def build(
    works: list[dict],
    run_date: str,
    *,
    topic_name: str = "",
    lookback_days: int = 0,
    first_run: bool = False,
    shown_count: int = 0,
    total_selected: int = 0,
    skipped: int = 0,
    sources: str = "",
) -> tuple[str, bytes]:
    """生成附件，返回 ``(文件名, PDF 字节)``。

    ``skipped`` 是"连附件都没收录进去"的篇数（超过 ``ATTACH_PDF_MAX_ITEMS``），
    会在 PDF 里明写出来，免得用户以为附件就是全部。
    """
    layout = _Layout()
    content_w = PAGE_W - 2 * MARGIN_X

    layout.blocks(
        _header_blocks(
            topic_name=topic_name,
            run_date=run_date,
            lookback_days=lookback_days,
            first_run=first_run,
            shown_count=shown_count,
            total_selected=total_selected,
            count=len(works),
            skipped=skipped,
            sources=sources,
        ),
        content_w,
    )
    layout.rule()

    for index, work in enumerate(works, start=1):
        layout.blocks(_item_blocks(work, index), content_w)
        layout.rule()

    filename = filename_for(topic_name, run_date, len(works))
    total_pages = len(layout.pages)
    for index, page in enumerate(layout.pages, start=1):
        page.hline(MARGIN_X, PAGE_W - MARGIN_X, MARGIN_BOTTOM - 14, 0.86, 0.5)
        page.text(MARGIN_X, MARGIN_BOTTOM - 26, 8.0, f"第 {index} / {total_pages} 页", 0.55)
        page.text(
            PAGE_W - MARGIN_X - _text_width("文献自动推送 · 自动生成", 8.0),
            MARGIN_BOTTOM - 26,
            8.0,
            "文献自动推送 · 自动生成",
            0.55,
        )

    payload = _assemble(
        layout.pages,
        font=config.ATTACH_PDF_FONT,
        encoding=config.ATTACH_PDF_ENCODING,
        ordering=config.ATTACH_PDF_CID_ORDERING,
        title=f"{topic_name} 超出正文上限的文献（{run_date}）",
    )
    log.debug(
        "PDF 附件已生成：%s（%s 篇，%s 页，%s 字节）", filename, len(works), total_pages, len(payload)
    )
    return filename, payload
