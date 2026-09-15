"""统一日志配置。

两个易踩的坑，这里都规避了：

1. **时区不一致**：原设计用 ``datetime.now()``（runner 上是 UTC）给日志文件命名，
   而业务日期用北京时间，两者会差一天。这里统一使用 :data:`src.config.TIMEZONE`。
2. **重复配置**：``logging.basicConfig`` 只在 root 尚无 handler 时生效，
   在函数里反复调用会静默失效。这里用模块级标志位保证只配置一次。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import LOG_DIR, TIMEZONE

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 这些第三方库的 DEBUG 日志非常吵，统一压到 WARNING
_NOISY_LOGGERS = ("urllib3", "requests", "charset_normalizer")


def now_local() -> datetime:
    """返回业务时区（默认 Asia/Shanghai）的当前时间。"""
    return datetime.now(ZoneInfo(TIMEZONE))


def today_str() -> str:
    """业务时区的 ``YYYY-MM-DD``，用于日志文件名与邮件标题。"""
    return now_local().strftime("%Y-%m-%d")


def setup_logging(verbose: bool = False) -> None:
    """挂载控制台 + 文件 handler。幂等，可安全重复调用。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{today_str()}.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # GitHub Actions 的日志里带 ANSI 颜色码很难看，显式关闭
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    root.debug("日志已初始化 → %s", log_path)


def get_logger(name: str) -> logging.Logger:
    """获取 logger。可在模块顶层调用，此时 ``setup_logging`` 尚未执行也没关系，
    日志会先缓存到 root，等 main() 配置好 handler 后再统一输出。"""
    return logging.getLogger(name)
