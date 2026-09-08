"""日志：loguru + 文件轮转。"""
from __future__ import annotations

import sys


def setup_logging(level: str = "INFO", sink: str | None = None) -> None:
    from loguru import logger

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan> - {message}",
    )
    if sink:
        logger.add(sink, level=level, rotation="50 MB", retention="30 days", enqueue=True)
