"""
OpenAI Sentinel Token 生成（纯 Python 方案）。

集成自 https://github.com/zc-zhangchen/any-auto-register
（platforms/chatgpt/sentinel_token.py，MIT License）。

旧实现见 sentinel_v1_legacy.py：先在本地 SHA3-512 算 PoW 再发，被 OpenAI 服务端
silent-drop（200 OK 但不下发 OTP）。新实现的关键区别：
  1. 第一次 POST /sentinel/req 发 `requirements_token`（不带真 PoW），仅声明
     config schema。
  2. 服务端返回 `{token, proofofwork: {required, seed, difficulty}}`，
     我们用 **服务端给的 seed/difficulty** 跑 FNV-1a 32-bit PoW。
  3. 第二次 PoW 解出来的 token 拼进 `{p, t:"", c: server_token, id, flow}` 才合法。
  4. SDK 版本字符串从旧的 `prod-f501fe...` 升级到当前的
     `https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js`。

公开 API `get_sentinel_token(session, device_id, flow, user_agent)` 保持不变，
auth_flow.py 的 4 处调用点不需要改。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = (
    '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
)

# Windows 精简 Python 环境可能没有系统 tzdata；这些常见出口的固定偏移只作
# 纯 Python Sentinel 兜底，Linux/完整 Python 仍优先使用 ZoneInfo（含 DST）。
_FALLBACK_TZ_HOURS = {
    "Asia/Tokyo": 9, "Asia/Seoul": 9, "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8, "Asia/Taipei": 8, "Asia/Singapore": 8,
    "Asia/Kuala_Lumpur": 8, "Asia/Bangkok": 7, "Asia/Ho_Chi_Minh": 7,
    "Asia/Manila": 8, "Asia/Jakarta": 7, "Asia/Kolkata": 5.5,
    "Asia/Karachi": 5, "Asia/Yekaterinburg": 5, "Asia/Novosibirsk": 7,
    "Asia/Dhaka": 6, "Asia/Jerusalem": 2, "Europe/Istanbul": 3,
    "Asia/Riyadh": 3, "Asia/Dubai": 4, "Europe/London": 0,
    "Europe/Berlin": 1, "Europe/Paris": 1, "Europe/Rome": 1,
    "Europe/Madrid": 1, "Europe/Amsterdam": 1, "Europe/Brussels": 1,
    "Europe/Zurich": 1, "Europe/Stockholm": 1, "Europe/Oslo": 1,
    "Europe/Copenhagen": 1, "Europe/Helsinki": 2, "Europe/Warsaw": 1,
    "Europe/Moscow": 3, "Europe/Kyiv": 2, "Europe/Prague": 1,
    "Europe/Vienna": 1, "Europe/Athens": 2, "Europe/Lisbon": 0,
    "America/New_York": -5, "America/Los_Angeles": -8,
    "America/Chicago": -6, "America/Denver": -7, "America/Toronto": -5,
    "America/Vancouver": -8, "America/Edmonton": -7,
    "America/Mexico_City": -6, "America/Sao_Paulo": -3,
    "America/Manaus": -4, "America/Fortaleza": -3,
    "America/Argentina/Buenos_Aires": -3, "America/Santiago": -4,
    "America/Bogota": -5, "Australia/Sydney": 10,
    "Australia/Melbourne": 10, "Australia/Brisbane": 10,
    "Pacific/Auckland": 12, "Africa/Johannesburg": 2,
    "Africa/Cairo": 2, "Africa/Lagos": 1, "Africa/Nairobi": 3,
}


class SentinelTokenGenerator:
    """Sentinel Token 纯 Python 生成器。

    - 不依赖 Node / JS。
    - `t` 字段固定空串；上游接口（`/sentinel/req`）的返回会判定能否接受。
    """

    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        device_id: str | None = None,
        user_agent: str | None = None,
        screen: str = "",
        lang: str = "",
        lang_full: str = "",
        browser_type: str = "",
        navigator_platform: str = "",
        navigator_vendor: str | None = None,
        hardware_concurrency: int = 0,
        device_memory: int | None = None,
        max_touch_points: int = 0,
        device_pixel_ratio: float = 0.0,
        timezone_name: str = "UTC",
    ):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or DEFAULT_UA
        self.screen = screen or "1920x1080"
        self.lang = lang or "en-US"
        self.lang_full = lang_full or "en-US,en"
        ua_lower = self.user_agent.lower()
        if not navigator_platform:
            if "iphone" in ua_lower:
                navigator_platform = "iPhone"
            elif "mac" in ua_lower:
                navigator_platform = "MacIntel"
            else:
                navigator_platform = "Win32"
        if navigator_vendor is None:
            if "firefox" in ua_lower:
                navigator_vendor = ""
            elif "chrome" in ua_lower:
                navigator_vendor = "Google Inc."
            else:
                navigator_vendor = "Apple Computer, Inc."
        self.browser_type = browser_type or ""
        self.navigator_platform = navigator_platform
        self.navigator_vendor = navigator_vendor
        self.hardware_concurrency = int(hardware_concurrency) if hardware_concurrency else 8
        self.device_memory = device_memory
        self.max_touch_points = int(max_touch_points)
        self.device_pixel_ratio = float(device_pixel_ratio) if device_pixel_ratio else 1.0
        self.timezone_name = timezone_name or "UTC"
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        try:
            now = datetime.now(ZoneInfo(self.timezone_name))
            offset = now.strftime("%z") or "+0000"
            date_str = now.strftime("%a %b %d %Y %H:%M:%S") + f" GMT{offset} ({self.timezone_name})"
        except Exception:
            hours = _FALLBACK_TZ_HOURS.get(self.timezone_name, 0)
            now = datetime.now(timezone.utc) + timedelta(hours=hours)
            sign = "+" if hours >= 0 else "-"
            absolute_minutes = int(abs(hours) * 60)
            offset = f"{sign}{absolute_minutes // 60:02d}{absolute_minutes % 60:02d}"
            date_str = now.strftime("%a %b %d %Y %H:%M:%S") + f" GMT{offset} ({self.timezone_name})"
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_props = [
            ("vendorSub", ""),
            ("productSub", "20030107"),
            ("vendor", self.navigator_vendor),
            ("platform", self.navigator_platform),
            ("maxTouchPoints", str(self.max_touch_points)),
            ("hardwareConcurrency", str(self.hardware_concurrency)),
            ("cookieEnabled", "true"),
            ("doNotTrack", random.choice(("null", "1", "unspecified"))),
            ("pdfViewerEnabled", "true" if self.navigator_vendor == "Google Inc." else "false"),
            ("userActivation", "undefined"),
            ("geolocation", "undefined"),
            ("plugins", "undefined"),
            ("mimeTypes", "undefined"),
            ("credentials", "undefined"),
            ("mediaDevices", "undefined"),
            ("permissions", "undefined"),
            ("locks", "undefined"),
        ]
        if self.device_memory is not None:
            nav_props.append(("deviceMemory", str(self.device_memory)))
        nav_prop, nav_value = random.choice(nav_props)
        return [
            self.screen,
            date_str,
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            self.lang,
            self.lang_full,
            random.random(),
            f"{nav_prop}−{nav_value}",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            self.hardware_concurrency,
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        encoded = self._base64_encode(config)
        digest = self._fnv1a_32(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return encoded + "~S"
        return None

    def generate_token(self, seed: str | None = None, difficulty: str | None = None) -> str:
        seed = seed or self.requirements_seed
        difficulty = difficulty or "0"
        start_time = time.time()
        config = self._get_config()
        for nonce in range(self.MAX_ATTEMPTS):
            value = self._run_check(start_time, seed, difficulty, config, nonce)
            if value:
                return "gAAAAAB" + value
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    sec_ch_ua_platform: str = "",
    sec_ch_ua_mobile: str = "",
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str | None = None,
    sec_ch_ua_platform_version: str = "",
    impersonate: str | None = None,
    request_p: str | None = None,
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    navigator_platform: str = "",
    navigator_vendor: str | None = None,
    hardware_concurrency: int = 0,
    device_memory: int | None = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone_name: str = "UTC",
) -> dict | None:
    """POST `/sentinel/req` 并返回响应 JSON。失败返回 None。"""
    generator = SentinelTokenGenerator(
        device_id=device_id, user_agent=user_agent,
        screen=screen, lang=lang, lang_full=lang_full,
        browser_type=browser_type,
        navigator_platform=navigator_platform,
        navigator_vendor=navigator_vendor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        device_pixel_ratio=device_pixel_ratio,
        timezone_name=timezone_name,
    )
    req_body = {
        "p": str(request_p or "").strip() or generator.generate_requirements_token(),
        "id": device_id,
        "flow": flow,
    }
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": SENTINEL_REFERER,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or DEFAULT_UA,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
        headers["sec-ch-ua-mobile"] = sec_ch_ua_mobile or "?0"
        headers["sec-ch-ua-platform"] = sec_ch_ua_platform or '"Windows"'
        for value, header_name in (
            (sec_ch_ua_full_version_list, "sec-ch-ua-full-version-list"),
            (sec_ch_ua_arch, "sec-ch-ua-arch"),
            (sec_ch_ua_bitness, "sec-ch-ua-bitness"),
            (sec_ch_ua_model, "sec-ch-ua-model"),
            (sec_ch_ua_platform_version, "sec-ch-ua-platform-version"),
        ):
            if value is not None and value != "":
                headers[header_name] = value
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        response = session.post(SENTINEL_REQ_URL, **kwargs)
        if response.status_code == 200:
            return response.json()
        logger.warning(f"Sentinel /req 非 200: {response.status_code}")
    except Exception as exc:
        logger.warning(f"Sentinel /req 异常: {exc}")
        return None
    return None


def build_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    sec_ch_ua_platform: str = "",
    sec_ch_ua_mobile: str = "",
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str | None = None,
    sec_ch_ua_platform_version: str = "",
    impersonate: str | None = None,
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    navigator_platform: str = "",
    navigator_vendor: str | None = None,
    hardware_concurrency: int = 0,
    device_memory: int | None = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone_name: str = "UTC",
) -> str | None:
    """完整 Sentinel token：fetch challenge → 用 server-given seed/difficulty 解 PoW → 拼装。

    返回 JSON 字符串，失败返回 None。
    """
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=sec_ch_ua_platform,
        sec_ch_ua_mobile=sec_ch_ua_mobile,
        sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
        sec_ch_ua_arch=sec_ch_ua_arch,
        sec_ch_ua_bitness=sec_ch_ua_bitness,
        sec_ch_ua_model=sec_ch_ua_model,
        sec_ch_ua_platform_version=sec_ch_ua_platform_version,
        impersonate=impersonate,
        screen=screen,
        lang=lang,
        lang_full=lang_full,
        browser_type=browser_type,
        navigator_platform=navigator_platform,
        navigator_vendor=navigator_vendor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        device_pixel_ratio=device_pixel_ratio,
        timezone_name=timezone_name,
    )
    if not challenge:
        return None

    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        logger.warning("Sentinel 响应缺 token 字段")
        return None

    generator = SentinelTokenGenerator(
        device_id=device_id, user_agent=user_agent,
        screen=screen, lang=lang, lang_full=lang_full,
        browser_type=browser_type,
        navigator_platform=navigator_platform,
        navigator_vendor=navigator_vendor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        device_pixel_ratio=device_pixel_ratio,
        timezone_name=timezone_name,
    )
    pow_data = challenge.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = generator.generate_requirements_token()

    payload = {
        "p": p_value,
        "t": "",
        "c": c_value,
        "id": device_id,
        "flow": flow,
    }
    return json.dumps(payload, separators=(",", ":"))


def get_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str = DEFAULT_UA,
    sec_ch_ua: str = "",
    sec_ch_ua_platform: str = "",
    sec_ch_ua_mobile: str = "",
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str | None = None,
    sec_ch_ua_platform_version: str = "",
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    navigator_platform: str = "",
    navigator_vendor: str | None = None,
    hardware_concurrency: int = 0,
    device_memory: int | None = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone: str = "UTC",
) -> str:
    """auth_flow.py 复用的入口；返回 JSON 字符串，永远不抛异常。

    优先级:
      1. QuickJS 路径 (`sentinel_quickjs.get_sentinel_token_via_quickjs`)
         在 Node 子进程里跑 OpenAI 真实 sdk.js，返回服务端可深层校验的 token。
         这是 OTP 邮件能下发的关键。
      2. 纯 Python 路径 (`build_sentinel_token`) — 只做表层 PoW，能通过
         200 OK 但 OpenAI 服务端 OTP 派发会 silent-drop。仅作为 Node 不可用
         或 sdk.js 下载失败时的兜底。
      3. 无 challenge 模式 — 最后兜底，保证返回可解析字符串避免阻塞。

    禁用 QuickJS：`export OPENAI_SENTINEL_DISABLE_QUICKJS=1`。
    """
    if not os.environ.get("OPENAI_SENTINEL_DISABLE_QUICKJS"):
        try:
            from sentinel_quickjs import get_sentinel_token_via_quickjs
            qtoken = get_sentinel_token_via_quickjs(
                session,
                device_id=device_id,
                flow=flow,
                log=lambda m: logger.info(m),
                user_agent=user_agent,
                screen=screen,
                lang=lang,
                lang_full=lang_full,
                browser_type=browser_type,
                platform=navigator_platform,
                vendor=navigator_vendor,
                hardware_concurrency=hardware_concurrency,
                device_memory=device_memory,
                max_touch_points=max_touch_points,
                device_pixel_ratio=device_pixel_ratio,
                timezone=timezone,
                sec_ch_ua=sec_ch_ua,
                sec_ch_ua_platform=sec_ch_ua_platform,
                sec_ch_ua_mobile=sec_ch_ua_mobile,
                sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
                sec_ch_ua_arch=sec_ch_ua_arch,
                sec_ch_ua_bitness=sec_ch_ua_bitness,
                sec_ch_ua_model=sec_ch_ua_model,
                sec_ch_ua_platform_version=sec_ch_ua_platform_version,
            )
            if qtoken:
                return qtoken
            logger.warning("Sentinel QuickJS 失败，回退到纯 Python")
        except Exception as e:
            logger.warning(f"Sentinel QuickJS 加载/调用异常，回退到纯 Python: {e}")

    token = build_sentinel_token(
        session,
        device_id=device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=sec_ch_ua_platform,
        sec_ch_ua_mobile=sec_ch_ua_mobile,
        sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
        sec_ch_ua_arch=sec_ch_ua_arch,
        sec_ch_ua_bitness=sec_ch_ua_bitness,
        sec_ch_ua_model=sec_ch_ua_model,
        sec_ch_ua_platform_version=sec_ch_ua_platform_version,
        screen=screen,
        lang=lang,
        lang_full=lang_full,
        browser_type=browser_type,
        navigator_platform=navigator_platform,
        navigator_vendor=navigator_vendor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        device_pixel_ratio=device_pixel_ratio,
        timezone_name=timezone,
    )
    if token:
        logger.debug(f"Sentinel Token (纯Python len={len(token)})")
        return token

    logger.warning("Sentinel /req 也失败，回退到无 challenge 模式")
    fallback_p = SentinelTokenGenerator(
        device_id=device_id, user_agent=user_agent,
        screen=screen, lang=lang, lang_full=lang_full,
        browser_type=browser_type,
        navigator_platform=navigator_platform,
        navigator_vendor=navigator_vendor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        device_pixel_ratio=device_pixel_ratio,
        timezone_name=timezone,
    ).generate_requirements_token()
    return json.dumps({
        "p": fallback_p,
        "t": "",
        "c": "",
        "id": device_id,
        "flow": flow,
    }, separators=(",", ":"))
