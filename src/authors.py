"""第一作者 / 第一通讯作者：抽取、存储与显示。

数据来源与它们的能力边界（2026-09 实测）
---------------------------------------
===================  ==============================  ==========  ==========
数据源               字段                            第一作者    第一通讯
===================  ==============================  ==========  ==========
OpenAlex             ``authorships[]``              有          有
Crossref             ``author[]``                   有          无
Semantic Scholar     ``authors[]``                  有          无
===================  ==============================  ==========  ==========

* **只有 OpenAlex 提供 ``is_corresponding``**，所以某一篇文献若只被
  Crossref / S2 召回到，卡片上就只显示一作 —— 这是数据源的边界，不是 bug。
* **绝不用"猜"**：不拿末位作者冒充通讯作者。猜错的作者信息比没有更糟，
  用户会当成真的去用。
* **「共同一作（共一）」三个源都没有**：那是出版社在 PDF 脚注里标的文字，
  元数据里根本不存在这个字段，因此不做（也无法做）共一识别。
* 显示规则（用户 2026-09-16 拍板）：**一作 + 一通讯，最多 2 个名字**；
  两者是同一人时合并成一条。
"""

from __future__ import annotations

import re

# 本模块是**纯函数**集合：没有配置、没有网络、没有 logger，便于离线单测。

_WS_RE = re.compile(r"\s+")
#: 判断"是不是同一个人"时忽略这些字符（``J. Kim`` vs ``J Kim``）
_NAME_NOISE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def clean_name(raw: object) -> str:
    """把作者名压成一行、去掉首尾空白与多余空格。空值返回 ``""``。"""
    if raw is None:
        return ""
    return _WS_RE.sub(" ", str(raw)).strip()


def join_name(given: object, family: object, fallback: object = "") -> str:
    """把 ``given`` + ``family`` 拼成一个姓名；都为空时用 ``fallback``。

    Crossref 用的是 ``given`` / ``family``（机构作者则只有 ``name``），
    这里统一成 ``"Given Family"``（西方惯例，与 OpenAlex 的
    ``raw_author_name`` 写法一致，便于去重比较）。
    """
    given_text = clean_name(given)
    family_text = clean_name(family)
    joined = clean_name(f"{given_text} {family_text}")
    return joined or clean_name(fallback)


def same_person(left: object, right: object) -> bool:
    """两个名字是否指同一个人（忽略大小写、点号、逗号、空格）。"""
    a = _NAME_NOISE_RE.sub("", str(left or "").lower())
    b = _NAME_NOISE_RE.sub("", str(right or "").lower())
    return bool(a) and a == b


def author_line(work: dict, *, max_names: int = 2) -> str:
    """卡片上那行作者。

    可能的输出（用户拍板：最多 2 个名字）::

        "张三（一作）· 李四（通讯）"
        "张三（一作兼通讯）"          # 一作与通讯同一个人，合并成一条
        "张三（一作）"                # 只有 Crossref / S2 命中时，通讯未知
        "李四（通讯）"                # 来源只标了通讯
        ""                            # 一无所知 —— 不输出空行

    刻意**不**输出"通讯：未知"之类的占位：邮件卡片上每多一个"未知"，
    用户就要多读一次才知道那是缺数据。缺就干脆不写。
    """
    first = clean_name(work.get("first_author"))
    corresponding = clean_name(work.get("corresponding_author"))

    if first and corresponding and same_person(first, corresponding):
        parts = [f"{first}（一作兼通讯）"]
    else:
        parts = []
        if first:
            parts.append(f"{first}（一作）")
        if corresponding:
            parts.append(f"{corresponding}（通讯）")

    limit = max(1, int(max_names))
    return " · ".join(parts[:limit])
