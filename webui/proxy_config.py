"""代理国家选项和动态会话模板工具。"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import urllib.parse
import urllib.request


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


def build_api_proxy_url(api_url: str, country: str) -> str:
    """将页面选择的国家写入 711 API，并强制返回可解析的 HTTP JSON。"""
    parts = urllib.parse.urlsplit(str(api_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("711 API URL 无效")
    params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    params.update({
        "count": "1",
        "proto": "http",
        "stype": "json",
        "region": normalize_country(country),
        "sessType": "sticky",
        "sessTime": "5",
        "sessAuto": "0",
    })
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def fetch_api_proxy(api_url: str, country: str, timeout: float = 20.0) -> str:
    """调用白名单 API 并返回一个无账号密码的 HTTP 代理。"""
    request_url = build_api_proxy_url(api_url, country)
    with urllib.request.urlopen(request_url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if int(payload.get("code") or 0) != 200 or payload.get("success") != "success":
        raise RuntimeError(f"711 API 返回失败: {payload.get('msg') or payload.get('code')}")
    rows = payload.get("data") or []
    if not rows or not isinstance(rows[0], dict):
        raise RuntimeError("711 API 未返回代理")
    host = str(rows[0].get("ip") or "").strip()
    port = int(rows[0].get("port") or 0)
    ipaddress.ip_address(host)
    if not 1 <= port <= 65535:
        raise RuntimeError("711 API 返回的端口无效")
    return f"http://{host}:{port}"


def _proxy_country_matches(proxy: str, country: str, timeout: float = 10.0) -> bool:
    try:
        from curl_cffi import requests

        response = requests.get(
            "https://ipwho.is/",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome136",
            timeout=timeout,
        )
        actual = str(response.json().get("country_code") or "").upper()
        return actual == normalize_country(country)
    except Exception:
        return False


def pick_working_proxy(
    proxy_template: str,
    *,
    attempts: int = 3,
    timeout: float = 10.0,
    api_url: str = "",
    country: str = "",
) -> str:
    """账号密码代理优先；连续失败后按所选国家调用白名单 API 兜底。"""
    template = str(proxy_template or "").strip()
    if not template:
        return ""
    candidate = materialize_proxy(template)
    for index in range(max(1, int(attempts or 1))):
        if index:
            candidate = materialize_proxy(template)
        if _proxy_reachable(candidate, timeout):
            return candidate
    if api_url and country:
        for _ in range(max(1, int(attempts or 1))):
            try:
                api_proxy = fetch_api_proxy(api_url, country, max(10.0, timeout))
            except Exception:
                continue
            if _proxy_reachable(api_proxy, timeout) and _proxy_country_matches(
                api_proxy, country, timeout,
            ):
                return api_proxy
    return candidate


def country_options_payload() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in COUNTRY_OPTIONS]
