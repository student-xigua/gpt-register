"""代理国家选项和动态会话模板工具。"""
from __future__ import annotations

import re
import secrets


COUNTRY_OPTIONS = (
    ("JP", "日本"),
    ("BR", "巴西"),
    ("IN", "印度"),
    ("KR", "韩国"),
    ("VN", "越南"),
    ("US", "美国"),
    ("SG", "新加坡"),
    ("GB", "英国"),
)
SUPPORTED_COUNTRIES = frozenset(code for code, _ in COUNTRY_OPTIONS)
POOL_COUNTRY_DEFAULTS = {
    "upi_pool1": "IN",
    "upi_pool2": "BR",
    "kakao_pool1": "KR",
    "kakao_pool2": "JP",
}
SID_PLACEHOLDER = "{sid}"
COUNTRY_PLACEHOLDER = "{country}"
_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)


def normalize_country(country: str, default: str = "") -> str:
    code = str(country or "").strip().upper()
    if code in SUPPORTED_COUNTRIES:
        return code
    fallback = str(default or "").strip().upper()
    if fallback in SUPPORTED_COUNTRIES:
        return fallback
    raise ValueError(f"不支持的代理国家: {country}")


def country_from_proxy(proxy: str, default: str = "") -> str:
    match = _COUNTRY_SELECTOR_RE.search(str(proxy or ""))
    if match:
        code = str(match.group("value") or "").split(",", 1)[0].upper()
        if code in SUPPORTED_COUNTRIES:
            return code
    return normalize_country(default) if default else ""


def set_proxy_country(proxy: str, country: str) -> str:
    text = str(proxy or "").strip()
    code = normalize_country(country)
    text = text.replace(COUNTRY_PLACEHOLDER, code)
    match = _COUNTRY_SELECTOR_RE.search(text)
    if not match:
        return text
    value = code if match.group("value").isupper() else code.lower()
    replacement = f"{match.group('name')}{match.group('separator')}{value}"
    return _COUNTRY_SELECTOR_RE.sub(replacement, text, count=1)


def materialize_proxy(proxy: str, *, country: str = "", sid: str = "") -> str:
    text = str(proxy or "").strip()
    if country:
        text = set_proxy_country(text, country)
    if SID_PLACEHOLDER in text:
        text = text.replace(SID_PLACEHOLDER, sid or secrets.token_hex(5))
    return text


def country_options_payload() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in COUNTRY_OPTIONS]
