"""代理国家选项和动态会话模板工具。"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import string
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


def new_sid_for_proxy(proxy: str) -> str:
    """按代理商格式生成 sticky SID。

    711Proxy 接受 8 位数字；1024Proxy 的账号模式要求 8 位大小写字母/数字，
    纯数字 SID 在其网关上会触发 TLS/连接失败。
    """
    hostname = (urllib.parse.urlsplit(str(proxy or "").strip()).hostname or "").lower()
    if hostname.endswith("1024proxy.io"):
        alphabet = string.ascii_letters + string.digits
        value = [secrets.choice(alphabet) for _ in range(8)]
        if not any(char.isalpha() for char in value):
            value[secrets.randbelow(len(value))] = secrets.choice(string.ascii_letters)
        return "".join(value)
    return new_sid()


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
        text = text.replace(SID_PLACEHOLDER, sid or new_sid_for_proxy(text))
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


def _proxy_urls_reachable(proxy: str, urls: tuple[str, ...], timeout: float = 10.0) -> bool:
    """验证同一 sticky 出口能否连接业务链路中的全部目标。"""
    try:
        from curl_cffi import requests

        for url in urls:
            response = requests.get(
                url,
                proxies={"http": proxy, "https": proxy},
                impersonate="chrome136",
                timeout=timeout,
            )
            status = int(response.status_code)
            if status == 407 or status >= 500:
                return False
        return True
    except Exception:
        return False


def build_api_proxy_url(api_url: str, country: str) -> str:
    """将页面选择的国家写入代理商 API，并规范为 HTTP 单节点返回。"""
    parts = urllib.parse.urlsplit(str(api_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("代理 API URL 无效")
    params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    hostname = (parts.hostname or "").lower()
    if "1024proxy.com" in hostname:
        # 1024 白名单 API 返回纯文本 ip:port；region 必须由页面国家下拉覆盖。
        params.update({
            "region": normalize_country(country),
            "num": "1",
            "time": params.get("time") or "10",
            "format": "1",
            "type": "txt",
        })
        query = urllib.parse.urlencode(params)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
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
    """调用白名单 API 并返回一个无账号密码的 HTTP 代理。

    同时兼容 711 的 JSON 响应和 1024Proxy 的纯文本 ``ip:port`` 响应。
    """
    request_url = build_api_proxy_url(api_url, country)
    # 1024 白名单端点会拒绝 Python urllib 的默认 User-Agent；显式使用通用 UA。
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    hostname = (urllib.parse.urlsplit(request_url).hostname or "").lower()
    if "1024proxy.com" in hostname:
        for line in raw.splitlines():
            value = line.strip()
            if not value:
                continue
            # 1024 txt 模式为 ip:port；兼容返回带 scheme 或 JSON 的情况。
            candidate = value
            if candidate.startswith(("http://", "https://")):
                candidate = urllib.parse.urlsplit(candidate).netloc
            if ":" not in candidate:
                continue
            host, port_text = candidate.rsplit(":", 1)
            try:
                ipaddress.ip_address(host.strip("[]"))
                port = int(port_text)
            except (ValueError, TypeError):
                continue
            if 1 <= port <= 65535:
                return f"http://{host}:{port}"
        raise RuntimeError("1024Proxy API 未返回有效的 ip:port")
    payload = json.loads(raw)
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
    probe_urls: tuple[str, ...] = (),
    verify_country: bool = False,
) -> str:
    """账号密码代理优先；连续失败后按所选国家调用白名单 API 兜底。"""
    template = str(proxy_template or "").strip()
    if not template:
        return ""
    candidate = materialize_proxy(template)
    for index in range(max(1, int(attempts or 1))):
        if index:
            candidate = materialize_proxy(template)
        reachable = (
            _proxy_urls_reachable(candidate, probe_urls, timeout)
            if probe_urls else _proxy_reachable(candidate, timeout)
        )
        if reachable and (
            not verify_country
            or not country
            or _proxy_country_matches(candidate, country, timeout)
        ):
            return candidate
    if api_url and country:
        for _ in range(max(1, int(attempts or 1))):
            try:
                api_proxy = fetch_api_proxy(api_url, country, max(10.0, timeout))
            except Exception:
                continue
            reachable = (
                _proxy_urls_reachable(api_proxy, probe_urls, timeout)
                if probe_urls else _proxy_reachable(api_proxy, timeout)
            )
            if reachable and _proxy_country_matches(
                api_proxy, country, timeout,
            ):
                return api_proxy
    return candidate


def country_options_payload() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in COUNTRY_OPTIONS]
