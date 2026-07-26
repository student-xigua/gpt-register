"""日志与用户可见错误的敏感信息脱敏工具。"""
from __future__ import annotations

import logging
import os
import re
import sys


_KEY_VALUE_RE = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:
            access_token|refresh_token|session_token|id_token|
            api_key|client_secret|totp_secret|secret|password|
            login_verifier|code_verifier|
            otp|otp_code|phone_number|activation_id
        )
        ["']?\s*[:=]\s*
    )
    (["']?)
    ([^"'&,\s}\]]+)
    \2
    """
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*)?\b")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:code|state|login_verifier|code_verifier)=)[^&#\s]+"
)
_LABELED_OTP_RE = re.compile(r"(?i)(\b(?:email|sms)?\s*OTP\s*[=:：]\s*)\d{4,8}")
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{100,}\b")


def redact_sensitive_text(value: object, *, max_length: int | None = None) -> str:
    """保留诊断语义，只替换敏感值；不会因字段名出现而隐藏整条错误。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = _KEY_VALUE_RE.sub(r"\1[hidden]", text)
    text = _BEARER_RE.sub("Bearer [hidden]", text)
    text = _JWT_RE.sub("[token hidden]", text)
    text = _QUERY_SECRET_RE.sub(r"\1[hidden]", text)
    text = _LABELED_OTP_RE.sub(r"\1[hidden]", text)
    text = _LONG_SECRET_RE.sub("[secret hidden]", text)
    if max_length is not None:
        text = text[:max_length]
    return text


class SensitiveDataFilter(logging.Filter):
    """对标准日志 handler 做最后一道兜底脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_sensitive_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def install_sensitive_data_filter() -> None:
    """安装到当前 root handlers；重复调用不会重复添加。"""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())


# ============================================================
# 控制台日志外观 + 降噪
# ============================================================

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[97;41m",
}
_RESET = "\033[0m"

# 高频/低价值日志源：轮询 + SSE 会让 uvicorn.access 刷屏，三方库的连接细节
# 也没多少诊断价值，统一压到 WARNING，突出业务日志。
_NOISY_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "urllib3", "asyncio")


class ConsoleFormatter(logging.Formatter):
    """`HH:MM:SS [LEVEL] name: msg`；仅在真正的 TTY 上着色。

    systemd/journald 捕获的是管道而非 TTY，此时自动降级为纯文本，
    不会把 ANSI 转义写进 journal。
    """

    def __init__(self, use_color: bool):
        super().__init__(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = _LEVEL_COLORS.get(record.levelname)
        return f"{color}{text}{_RESET}" if color else text


def setup_app_logging(level: int = logging.INFO, stream=None) -> None:
    """
    统一配置 WebUI 的控制台日志：装 formatter、脱敏过滤器、压三方噪声。

    替代裸 `logging.basicConfig`，让 CLI（TTY 彩色）和 systemd（纯文本）同一套
    代码各得其所。多次调用安全（会先清掉旧 handler）。
    """
    stream = stream or sys.stderr
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    use_color = bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ConsoleFormatter(use_color))
    root.addHandler(handler)

    install_sensitive_data_filter()

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
