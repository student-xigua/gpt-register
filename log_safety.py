"""日志与用户可见错误的敏感信息脱敏工具。"""
from __future__ import annotations

import logging
import re


_KEY_VALUE_RE = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:
            access_token|refresh_token|session_token|id_token|
            api_key|client_secret|password|
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
