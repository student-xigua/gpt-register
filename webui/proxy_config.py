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


def new_sid() -> str:
    """生成 711Proxy 官方格式要求的 8 位数字会话 ID。"""
    return f"{secrets.randbelow(100_000_000):08d}"


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
        text = text.replace(SID_PLACEHOLDER, sid or new_sid())
    return text


def _proxy_reachable(proxy: str, timeout: float = 10.0) -> bool:
    """用项目实际 TLS 客户端验证代理能否访问 ChatGPT。"""
    try:
        from curl_cffi import requests

        response = requests.get(
            "https://chatgpt.com/api/auth/csrf",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome136",
            timeout=timeout,
        )
        return 200 <= int(response.status_code) < 500 and int(response.status_code) != 407
    except Exception:
        return False


def pick_working_proxy(proxy_template: str, *, attempts: int = 3, timeout: float = 10.0) -> str:
    """预检动态住宅节点；坏节点自动换 SID，全部失败时返回最后一次供主流程报错。"""
    template = str(proxy_template or "").strip()
    if not template:
        return ""
    candidate = materialize_proxy(template)
    for index in range(max(1, int(attempts or 1))):
        if index:
            candidate = materialize_proxy(template)
        if _proxy_reachable(candidate, timeout):
            return candidate
    return candidate


def country_options_payload() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in COUNTRY_OPTIONS]
