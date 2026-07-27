"""浏览器指纹随机化（多浏览器家族）。

每次注册调用 generate_fingerprint() 生成一套一致的指纹组合：
  - TLS impersonate（curl_cffi 用）
  - User-Agent
  - sec-ch-ua / sec-ch-ua-platform / sec-ch-ua-mobile（仅 Chrome）
  - 屏幕分辨率
  - Accept-Language / IANA timezone（按出口 IP 国家联动）
  - browser_type 标识（mac_safari / ios_safari / chrome / firefox）
  - navigator / 硬件画像（与浏览器家族一致）
  - fallback_impersonates 同家族回退列表
"""
from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# macOS Safari（保留原有）
# ---------------------------------------------------------------------------
_SAFARI_VERSIONS = [
    {
        "impersonate": "safari15_3",
        "safari_ver": "15.3",
        "webkit_ver": "605.1.15",
        "macos_versions": ["10_15_7", "12_0", "12_1"],
    },
    {
        "impersonate": "safari15_5",
        "safari_ver": "15.5",
        "webkit_ver": "605.1.15",
        "macos_versions": ["10_15_7", "12_4", "12_5"],
    },
    {
        "impersonate": "safari17_0",
        "safari_ver": "17.0",
        "webkit_ver": "605.1.15",
        "macos_versions": ["13_6", "14_0", "14_1"],
    },
    {
        "impersonate": "safari18_0",
        "safari_ver": "18.0",
        "webkit_ver": "605.1.15",
        "macos_versions": ["14_4", "14_5", "15_0", "15_1"],
    },
]

_MAC_SCREENS = [
    "1440x900",
    "1512x982",
    "1728x1117",
    "2560x1440",
    "1920x1080",
]

# ---------------------------------------------------------------------------
# iOS Safari
# ---------------------------------------------------------------------------
_IOS_SAFARI_VERSIONS = [
    {
        "impersonate": "safari17_2_ios",
        "safari_ver": "17.2",
        "webkit_ver": "605.1.15",
        "ios_versions": ["17_1_2", "17_2"],
    },
    {
        "impersonate": "safari18_0_ios",
        "safari_ver": "18.0",
        "webkit_ver": "605.1.15",
        "ios_versions": ["18_0", "18_1", "18_1_1"],
    },
]

_IPHONE_SCREENS = [
    "390x844",   # iPhone 13 / 14
    "393x852",   # iPhone 14 Pro / 15
    "428x926",   # iPhone 13 Pro Max / 14 Plus
    "430x932",   # iPhone 14 Pro Max / 15 Plus
]

# ---------------------------------------------------------------------------
# Chrome (Windows)
# ---------------------------------------------------------------------------
_CHROME_VERSIONS = [
    {
        "impersonate": "chrome136",
        "ver": "136",
        "full_ver": "136.0.0.0",
        "not_a_brand": '"Not.A/Brand";v="99"',
    },
    {
        "impersonate": "chrome142",
        "ver": "142",
        "full_ver": "142.0.0.0",
        "not_a_brand": '"Not/A)Brand";v="8"',
    },
    {
        "impersonate": "chrome146",
        "ver": "146",
        "full_ver": "146.0.0.0",
        "not_a_brand": '"Not?A_Brand";v="99"',
    },
]

_WIN_SCREENS = [
    "1920x1080",
    "1366x768",
    "2560x1440",
    "1536x864",
    "1440x900",
]

# ---------------------------------------------------------------------------
# Firefox (Windows)
# ---------------------------------------------------------------------------
_FIREFOX_VERSIONS = [
    {"impersonate": "firefox133", "ver": "133.0"},
    {"impersonate": "firefox144", "ver": "144.0"},
]

# ---------------------------------------------------------------------------
# 共享
# ---------------------------------------------------------------------------
_LANGUAGES = [
    ("en-US", "en-US,en;q=0.9"),
    ("en-US", "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"),
    ("en-GB", "en-GB,en;q=0.9,en-US;q=0.8"),
    ("en-US", "en-US,en;q=0.9,ja;q=0.8"),
]

_BROWSER_WEIGHTS = [
    ("mac_safari", 30),
    ("ios_safari", 15),
    ("chrome",     35),
    ("firefox",    20),
]

_BROWSER_TYPES = [t for t, _ in _BROWSER_WEIGHTS]
_WEIGHTS = [w for _, w in _BROWSER_WEIGHTS]


# ---------------------------------------------------------------------------
# IP 国家 → 时区 / 语言画像
# ---------------------------------------------------------------------------

_COUNTRY_PROFILES: dict[str, dict] = {
    # 亚洲
    "JP": {"timezones": (("Asia/Tokyo", 1.0),), "languages": ("ja-JP", "ja", "en-US", "en", "zh-CN")},
    "CN": {"timezones": (("Asia/Shanghai", 1.0),), "languages": ("zh-CN", "zh", "en-US", "en")},
    "HK": {"timezones": (("Asia/Hong_Kong", 1.0),), "languages": ("zh-HK", "zh-CN", "zh", "en-US", "en")},
    "TW": {"timezones": (("Asia/Taipei", 1.0),), "languages": ("zh-TW", "zh", "en-US", "en", "ja")},
    "KR": {"timezones": (("Asia/Seoul", 1.0),), "languages": ("ko-KR", "ko", "en-US", "en", "ja")},
    "SG": {"timezones": (("Asia/Singapore", 1.0),), "languages": ("zh-CN", "zh", "en-US", "en", "ms-MY", "ms")},
    "MY": {"timezones": (("Asia/Kuala_Lumpur", 1.0),), "languages": ("ms-MY", "ms", "zh-CN", "zh", "en-US", "en")},
    "TH": {"timezones": (("Asia/Bangkok", 1.0),), "languages": ("th-TH", "th", "en-US", "en")},
    "VN": {"timezones": (("Asia/Ho_Chi_Minh", 1.0),), "languages": ("vi-VN", "vi", "en-US", "en")},
    "IN": {"timezones": (("Asia/Kolkata", 1.0),), "languages": ("en-IN", "en-US", "en", "hi-IN", "hi")},
    "ID": {"timezones": (("Asia/Jakarta", 1.0),), "languages": ("id-ID", "id", "en-US", "en")},
    "PH": {"timezones": (("Asia/Manila", 1.0),), "languages": ("en-US", "en", "tl-PH", "tl")},
    "PK": {"timezones": (("Asia/Karachi", 1.0),), "languages": ("en-US", "en", "ur-PK", "ur")},
    "BD": {"timezones": (("Asia/Dhaka", 1.0),), "languages": ("bn-BD", "bn", "en-US", "en")},
    "IL": {"timezones": (("Asia/Jerusalem", 1.0),), "languages": ("he-IL", "he", "en-US", "en", "ar")},
    "TR": {"timezones": (("Europe/Istanbul", 1.0),), "languages": ("tr-TR", "tr", "en-US", "en")},
    "SA": {"timezones": (("Asia/Riyadh", 1.0),), "languages": ("ar-SA", "ar", "en-US", "en")},
    "AE": {"timezones": (("Asia/Dubai", 1.0),), "languages": ("ar-AE", "ar", "en-US", "en")},
    # 美洲
    "US": {"timezones": (("America/New_York", 0.4), ("America/Los_Angeles", 0.3), ("America/Chicago", 0.2), ("America/Denver", 0.1)), "languages": ("en-US", "en", "es-US", "es", "zh-CN")},
    "CA": {"timezones": (("America/Toronto", 0.6), ("America/Vancouver", 0.3), ("America/Edmonton", 0.1)), "languages": ("en-CA", "en-US", "en", "fr-CA", "fr")},
    "MX": {"timezones": (("America/Mexico_City", 1.0),), "languages": ("es-MX", "es", "en-US", "en")},
    "BR": {"timezones": (("America/Sao_Paulo", 0.7), ("America/Manaus", 0.2), ("America/Fortaleza", 0.1)), "languages": ("pt-BR", "pt", "en-US", "en", "es")},
    "AR": {"timezones": (("America/Argentina/Buenos_Aires", 1.0),), "languages": ("es-AR", "es", "en-US", "en")},
    "CL": {"timezones": (("America/Santiago", 1.0),), "languages": ("es-CL", "es", "en-US", "en")},
    "CO": {"timezones": (("America/Bogota", 1.0),), "languages": ("es-CO", "es", "en-US", "en")},
    # 欧洲
    "GB": {"timezones": (("Europe/London", 1.0),), "languages": ("en-GB", "en-US", "en", "fr", "de")},
    "DE": {"timezones": (("Europe/Berlin", 1.0),), "languages": ("de-DE", "de", "en-US", "en", "fr")},
    "FR": {"timezones": (("Europe/Paris", 1.0),), "languages": ("fr-FR", "fr", "en-US", "en", "de")},
    "IT": {"timezones": (("Europe/Rome", 1.0),), "languages": ("it-IT", "it", "en-US", "en", "fr")},
    "ES": {"timezones": (("Europe/Madrid", 1.0),), "languages": ("es-ES", "es", "en-US", "en", "fr")},
    "NL": {"timezones": (("Europe/Amsterdam", 1.0),), "languages": ("nl-NL", "nl", "en-US", "en", "de")},
    "BE": {"timezones": (("Europe/Brussels", 1.0),), "languages": ("nl-BE", "fr-BE", "nl", "fr", "en-US", "en")},
    "CH": {"timezones": (("Europe/Zurich", 1.0),), "languages": ("de-CH", "fr-CH", "de", "fr", "it", "en-US", "en")},
    "SE": {"timezones": (("Europe/Stockholm", 1.0),), "languages": ("sv-SE", "sv", "en-US", "en")},
    "NO": {"timezones": (("Europe/Oslo", 1.0),), "languages": ("nb-NO", "nb", "en-US", "en")},
    "DK": {"timezones": (("Europe/Copenhagen", 1.0),), "languages": ("da-DK", "da", "en-US", "en")},
    "FI": {"timezones": (("Europe/Helsinki", 1.0),), "languages": ("fi-FI", "fi", "sv", "en-US", "en")},
    "PL": {"timezones": (("Europe/Warsaw", 1.0),), "languages": ("pl-PL", "pl", "en-US", "en")},
    "RU": {"timezones": (("Europe/Moscow", 0.7), ("Asia/Yekaterinburg", 0.15), ("Asia/Novosibirsk", 0.15)), "languages": ("ru-RU", "ru", "en-US", "en")},
    "UA": {"timezones": (("Europe/Kyiv", 1.0),), "languages": ("uk-UA", "uk", "ru", "en-US", "en")},
    "CZ": {"timezones": (("Europe/Prague", 1.0),), "languages": ("cs-CZ", "cs", "en-US", "en", "de")},
    "AT": {"timezones": (("Europe/Vienna", 1.0),), "languages": ("de-AT", "de", "en-US", "en")},
    "GR": {"timezones": (("Europe/Athens", 1.0),), "languages": ("el-GR", "el", "en-US", "en")},
    "PT": {"timezones": (("Europe/Lisbon", 1.0),), "languages": ("pt-PT", "pt", "en-US", "en", "es")},
    # 大洋洲 / 非洲
    "AU": {"timezones": (("Australia/Sydney", 0.5), ("Australia/Melbourne", 0.3), ("Australia/Brisbane", 0.2)), "languages": ("en-AU", "en-US", "en", "zh-CN", "zh")},
    "NZ": {"timezones": (("Pacific/Auckland", 1.0),), "languages": ("en-NZ", "en-US", "en")},
    "ZA": {"timezones": (("Africa/Johannesburg", 1.0),), "languages": ("en-ZA", "en-US", "en", "af")},
    "EG": {"timezones": (("Africa/Cairo", 1.0),), "languages": ("ar-EG", "ar", "en-US", "en")},
    "NG": {"timezones": (("Africa/Lagos", 1.0),), "languages": ("en-NG", "en-US", "en")},
    "KE": {"timezones": (("Africa/Nairobi", 1.0),), "languages": ("sw-KE", "sw", "en-US", "en")},
}


# navigator / 硬件值按浏览器引擎绑定；同一个 fp 生命周期内不再改变。
_HARDWARE_PROFILES = {
    "mac_safari": {
        "navigator_platform": "MacIntel", "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": (8, 10, 12, 16), "device_memory": (None,),
        "max_touch_points": (0,), "device_pixel_ratio": (2.0,),
    },
    "ios_safari": {
        "navigator_platform": "iPhone", "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": (4, 6), "device_memory": (None,),
        "max_touch_points": (5,), "device_pixel_ratio": (2.0, 3.0),
    },
    "chrome": {
        "navigator_platform": "Win32", "navigator_vendor": "Google Inc.",
        "hardware_concurrency": (4, 6, 8, 12, 16, 24), "device_memory": (4, 8),
        "max_touch_points": (0,), "device_pixel_ratio": (1.0, 1.25, 1.5),
    },
    "firefox": {
        "navigator_platform": "Win32", "navigator_vendor": "",
        "hardware_concurrency": (4, 6, 8, 12, 16), "device_memory": (None,),
        "max_touch_points": (0,), "device_pixel_ratio": (1.0, 1.5),
    },
}


def _apply_hardware_profile(fp: dict, r: random.Random) -> None:
    profile = _HARDWARE_PROFILES.get(fp["browser_type"], _HARDWARE_PROFILES["chrome"])
    fp["navigator_platform"] = profile["navigator_platform"]
    fp["navigator_vendor"] = profile["navigator_vendor"]
    fp["hardware_concurrency"] = r.choice(profile["hardware_concurrency"])
    fp["device_memory"] = r.choice(profile["device_memory"])
    fp["max_touch_points"] = r.choice(profile["max_touch_points"])
    fp["device_pixel_ratio"] = r.choice(profile["device_pixel_ratio"])


def apply_geo_profile(
    fp: dict,
    country_code: str,
    rng: random.Random | None = None,
) -> dict:
    """只更新地理相关字段，绝不改变既有 UA/TLS/浏览器/硬件画像。"""
    profile = _COUNTRY_PROFILES.get((country_code or "").strip().upper())
    if not profile:
        return fp

    r = rng or random
    timezone_items = profile["timezones"]
    fp["timezone"] = r.choices(
        [item[0] for item in timezone_items],
        weights=[item[1] for item in timezone_items],
        k=1,
    )[0]

    pool = list(profile["languages"])
    primary = pool[0]
    others = pool[1:]
    r.shuffle(others)
    count = r.randint(min(3, len(pool)), min(5, len(pool)))
    selected = [primary, *others[: max(0, count - 1)]]
    fp["lang"] = primary
    fp["lang_full"] = ",".join(
        lang if index == 0 else f"{lang};q={1.0 - index * 0.1:.1f}"
        for index, lang in enumerate(selected)
    )
    return fp


# ---------------------------------------------------------------------------
# 指纹生成
# ---------------------------------------------------------------------------

def _gen_mac_safari(r: random.Random) -> dict:
    safari = r.choice(_SAFARI_VERSIONS)
    macos_ver = r.choice(safari["macos_versions"])
    others = [s["impersonate"] for s in _SAFARI_VERSIONS if s["impersonate"] != safari["impersonate"]]
    return {
        "browser_type": "mac_safari",
        "impersonate": safari["impersonate"],
        "fallback_impersonates": [safari["impersonate"]] + r.sample(others, min(2, len(others))),
        "user_agent": (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {macos_ver}) "
            f"AppleWebKit/{safari['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{safari['safari_ver']} Safari/{safari['webkit_ver']}"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_MAC_SCREENS),
    }


def _gen_ios_safari(r: random.Random) -> dict:
    safari = r.choice(_IOS_SAFARI_VERSIONS)
    ios_ver = r.choice(safari["ios_versions"])
    others = [s["impersonate"] for s in _IOS_SAFARI_VERSIONS if s["impersonate"] != safari["impersonate"]]
    fallbacks = [safari["impersonate"]] + others
    return {
        "browser_type": "ios_safari",
        "impersonate": safari["impersonate"],
        "fallback_impersonates": fallbacks,
        "user_agent": (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
            f"AppleWebKit/{safari['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{safari['safari_ver']} Mobile/15E148 Safari/604.1"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_IPHONE_SCREENS),
    }


def _gen_chrome(r: random.Random) -> dict:
    chrome = r.choice(_CHROME_VERSIONS)
    others = [c["impersonate"] for c in _CHROME_VERSIONS if c["impersonate"] != chrome["impersonate"]]
    sec_ch_ua = (
        f'"Chromium";v="{chrome["ver"]}", '
        f'"Google Chrome";v="{chrome["ver"]}", '
        f'{chrome["not_a_brand"]}'
    )
    sec_ch_ua_full_version_list = (
        f'"Chromium";v="{chrome["full_ver"]}", '
        f'"Google Chrome";v="{chrome["full_ver"]}", '
        f'{chrome["not_a_brand"]}'
    )
    return {
        "browser_type": "chrome",
        "impersonate": chrome["impersonate"],
        "fallback_impersonates": [chrome["impersonate"]] + r.sample(others, min(2, len(others))),
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome['full_ver']} Safari/537.36"
        ),
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_full_version_list": sec_ch_ua_full_version_list,
        "sec_ch_ua_arch": '"x86"',
        "sec_ch_ua_bitness": '"64"',
        "sec_ch_ua_model": '""',
        "sec_ch_ua_platform_version": r.choice(('"10.0.0"', '"15.0.0"')),
        "screen": r.choice(_WIN_SCREENS),
    }


def _gen_firefox(r: random.Random) -> dict:
    ff = r.choice(_FIREFOX_VERSIONS)
    others = [f["impersonate"] for f in _FIREFOX_VERSIONS if f["impersonate"] != ff["impersonate"]]
    fallbacks = [ff["impersonate"]] + others
    return {
        "browser_type": "firefox",
        "impersonate": ff["impersonate"],
        "fallback_impersonates": fallbacks,
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ff['ver']}) "
            f"Gecko/20100101 Firefox/{ff['ver']}"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_WIN_SCREENS),
    }


_GENERATORS = {
    "mac_safari": _gen_mac_safari,
    "ios_safari": _gen_ios_safari,
    "chrome": _gen_chrome,
    "firefox": _gen_firefox,
}


def generate_fingerprint(rng: random.Random | None = None) -> dict:
    """生成一套一致的浏览器指纹。

    返回 dict:
        browser_type: str     — 浏览器家族
        impersonate: str      — curl_cffi TLS 指纹名
        fallback_impersonates: list[str] — 同家族回退 impersonate 列表
        user_agent: str       — 完整 UA 字符串
        sec_ch_ua: str        — Client Hints（仅 Chrome 非空）
        sec_ch_ua_platform: str
        sec_ch_ua_mobile: str
        sec_ch_ua_full_version_list / arch / bitness / model / platform_version
        screen: str           — 屏幕分辨率 (WxH)
        lang: str             — 主语言
        lang_full: str        — 完整 Accept-Language
        timezone: str         — IANA 时区
        navigator_platform / navigator_vendor / hardware_concurrency / ...
    """
    r = rng or random
    browser_type = r.choices(_BROWSER_TYPES, weights=_WEIGHTS, k=1)[0]
    fp = _GENERATORS[browser_type](r)

    lang, lang_full = r.choice(_LANGUAGES)
    fp["lang"] = lang
    fp["lang_full"] = lang_full
    fp["timezone"] = "UTC"
    _apply_hardware_profile(fp, r)

    # 非 Chromium 不发送 UA Client Hints；统一补键便于调用方安全读取。
    if browser_type != "chrome":
        fp.setdefault("sec_ch_ua_full_version_list", "")
        fp.setdefault("sec_ch_ua_arch", "")
        fp.setdefault("sec_ch_ua_bitness", "")
        fp.setdefault("sec_ch_ua_model", None)
        fp.setdefault("sec_ch_ua_platform_version", "")
    return fp


# ---------------------------------------------------------------------------
# impersonate → UA 映射（TLS 旋转用）
# ---------------------------------------------------------------------------

_ALL_IMPERSONATES: dict[str, dict] = {}

for s in _SAFARI_VERSIONS:
    _ALL_IMPERSONATES[s["impersonate"]] = {"type": "mac_safari", "data": s}
for s in _IOS_SAFARI_VERSIONS:
    _ALL_IMPERSONATES[s["impersonate"]] = {"type": "ios_safari", "data": s}
for c in _CHROME_VERSIONS:
    _ALL_IMPERSONATES[c["impersonate"]] = {"type": "chrome", "data": c}
for f in _FIREFOX_VERSIONS:
    _ALL_IMPERSONATES[f["impersonate"]] = {"type": "firefox", "data": f}


def sync_fingerprint_for_impersonate(
    fp: dict,
    impersonate: str,
    user_agent: str,
) -> dict:
    """TLS 同家族回退时同步 UA/Client Hints，保留地理与硬件字段。"""
    entry = _ALL_IMPERSONATES.get(impersonate)
    if not entry:
        return fp

    browser_type, data = entry["type"], entry["data"]
    fp["browser_type"] = browser_type
    fp["impersonate"] = impersonate
    fp["user_agent"] = user_agent
    if browser_type == "chrome":
        fp["sec_ch_ua"] = (
            f'"Chromium";v="{data["ver"]}", '
            f'"Google Chrome";v="{data["ver"]}", '
            f'{data["not_a_brand"]}'
        )
        fp["sec_ch_ua_full_version_list"] = (
            f'"Chromium";v="{data["full_ver"]}", '
            f'"Google Chrome";v="{data["full_ver"]}", '
            f'{data["not_a_brand"]}'
        )
        fp["sec_ch_ua_platform"] = '"Windows"'
        fp["sec_ch_ua_mobile"] = "?0"
        fp["sec_ch_ua_arch"] = '"x86"'
        fp["sec_ch_ua_bitness"] = '"64"'
        fp["sec_ch_ua_model"] = '""'
        fp.setdefault("sec_ch_ua_platform_version", '"10.0.0"')
    else:
        for key in (
            "sec_ch_ua", "sec_ch_ua_platform", "sec_ch_ua_mobile",
            "sec_ch_ua_full_version_list", "sec_ch_ua_arch",
            "sec_ch_ua_bitness", "sec_ch_ua_platform_version",
        ):
            fp[key] = ""
        fp["sec_ch_ua_model"] = None
    return fp


def ua_for_impersonate(impersonate: str, current_ua: str) -> str:
    """根据 impersonate 名生成匹配的 UA。"""
    entry = _ALL_IMPERSONATES.get(impersonate)
    if not entry:
        return current_ua

    t, d = entry["type"], entry["data"]

    if t == "mac_safari":
        macos_ver = random.choice(d["macos_versions"])
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {macos_ver}) "
            f"AppleWebKit/{d['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{d['safari_ver']} Safari/{d['webkit_ver']}"
        )
    elif t == "ios_safari":
        ios_ver = random.choice(d["ios_versions"])
        return (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
            f"AppleWebKit/{d['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{d['safari_ver']} Mobile/15E148 Safari/604.1"
        )
    elif t == "chrome":
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{d['full_ver']} Safari/537.36"
        )
    elif t == "firefox":
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{d['ver']}) "
            f"Gecko/20100101 Firefox/{d['ver']}"
        )
    return current_ua
