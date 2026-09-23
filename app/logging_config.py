"""日志配置：单测与服务共用同一套判定审计日志。"""
from __future__ import annotations

import logging
import sys

LOGGER_NAME = "risk.audit"
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"

_configured = False


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True
    return logger


def get_logger() -> logging.Logger:
    return configure_logging()
