"""为已注册账号提炼 UPI / Kakao Pay 付款链接。

移植自 pix-link-extractor（mode5_minimal_equivalent.replay_once）的核心提链流程，
只保留「单账号跑一遍拿到链接」所需的最小闭环：
    OpenAI checkout → Stripe init → OpenAI update（压促销价）→ Stripe 换 pm
    → confirm → OpenAI approve ∥ Stripe details 轮询 → 提取托管链接

不落任何调试文件；网络用 curl_cffi 模拟 Chrome TLS 过 Cloudflare。
UPI 走印度（IN）账单、kakao_pay 走韩国（KR）账单，两者只是参数不同。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from typing import Callable, Optional
from urllib.parse import quote, urljoin, urlsplit

from .exporter import _import_cffi

OAI_CHECKOUT = "https://chatgpt.com/backend-api/payments/checkout"
OAI_UPDATE = "https://chatgpt.com/backend-api/payments/checkout/update"
OAI_TAXES = "https://chatgpt.com/backend-api/payments/checkout/taxes"
OAI_APPROVE = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME_VERSION = "6f8494a281"
KAKAO_STRIPE_RUNTIME_VERSION = "c00af4ce81"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
PROMO_ID = "plus-1-month-free"
KAKAO_IP_CHECK_SOURCES = (
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipapi", "https://ipapi.co/json/"),
    ("ipwho", "https://ipwho.is/"),
    ("myip", "https://api.myip.com/"),
)
KAKAO_PROVIDER_DOMAINS = (
    "kakao.com",
    "kakaopay.com",
    "nicepay.co.kr",
)
KAKAO_REDIRECT_INTERMEDIATE_DOMAINS = ("stripe.com",)

# 各支付方式只是账单国家 / 货币 / 支付方式类型不同，流程完全一致。
METHODS: dict[str, dict] = {
    "upi": {
        "label": "UPI",
        "country": "IN",
        "currency": "INR",
        "locale": "en-IN",
        "timezone": "Asia/Kolkata",
        "accept_language": "en-IN,en;q=0.9",
        "pm_type": "upi",
        # UPI 只认 0 INR 促销链接，拿到全价说明促销没生效，直接失败别浪费轮询。
        "require_zero": True,
        "link_match": "/upi/instructions/",
    },
    "kakao": {
        "label": "Kakao Pay",
        "country": "KR",
        "currency": "KRW",
        "locale": "ko-KR",
        "elements_locale": "ko",
        "timezone": "Asia/Seoul",
        "accept_language": "ko-KR,ko;q=0.9,en;q=0.8",
        "pm_type": "kakao_pay",
        # Kakao 只接受促销生效后的 0 KRW checkout，并用同一 sticky Seed 派生地区。
        "require_zero": True,
        "promotion_country": "VN",
        "provider_country": "KR",
        "min_poll_seconds": 120,
        "link_match": "",
    },
}

_IN_FIRST = ("Aarav", "Aditya", "Arjun", "Karan", "Rahul", "Ananya", "Diya", "Priya", "Riya", "Sneha")
_IN_LAST = ("Agarwal", "Gupta", "Iyer", "Kapoor", "Mehta", "Nair", "Patel", "Rao", "Sharma", "Singh")
_IN_LOC = (
    ("Vaishali Nagar", "Jaipur", "302021", "Rajasthan"),
    ("Indiranagar", "Bengaluru", "560038", "Karnataka"),
    ("Madhapur", "Hyderabad", "500081", "Telangana"),
    ("Koregaon Park", "Pune", "411001", "Maharashtra"),
    ("Andheri West", "Mumbai", "400053", "Maharashtra"),
)
_KR_FIRST = ("Minjun", "Seojun", "Doyun", "Jiho", "Haeun", "Seoyeon", "Jiwoo", "Yuna", "Soeun", "Hayoon")
_KR_LAST = ("Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Jang", "Lim")
_KR_LOC = (
    ("Teheran-ro", "Seoul", "06232", "Seoul"),
    ("Haeundae-ro", "Busan", "48095", "Busan"),
    ("Dongseong-ro", "Daegu", "41911", "Daegu"),
    ("Songdo", "Incheon", "21984", "Incheon"),
    ("Dunsan-dong", "Daejeon", "35233", "Daejeon"),
)


def _profile(method: str) -> dict[str, str]:
    rng = random.Random()
    if method == "kakao":
        first, last = rng.choice(_KR_FIRST), rng.choice(_KR_LAST)
        area, city, postal, state = rng.choice(_KR_LOC)
    else:
        first, last = rng.choice(_IN_FIRST), rng.choice(_IN_LAST)
        area, city, postal, state = rng.choice(_IN_LOC)
    num = rng.randint(12, 4899)
    domain = rng.choice(("outlook.com", "gmail.com"))
    return {
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{rng.randint(10, 9999)}@{domain}",
        "country": METHODS[method]["country"],
        "line1": f"{num}, {area}",
        "city": city,
        "postal_code": postal,
        "state": state,
    }


PROXY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)


def _force_region(proxy: str, region: str) -> str:
    """把住宅代理用户名里的 region-XX 改成目标国家（cliproxy / 1024 等多地池）。

    approve/checkout 必须走账单国（IN/KR）出口，压价 update 走 BR。用户常把同一份
    IN 池粘到两个框，靠这个把 region 标签强制成正确国家；池里没有 region- 标签就原样返回。
    """
    text = (proxy or "").strip()
    code = (region or "").strip().upper()[:2]
    if not text or not code:
        return text
    match = PROXY_COUNTRY_SELECTOR_RE.search(text)
    if match:
        value = code if match.group("value").isupper() else code.lower()
        replacement = f"{match.group('name')}{match.group('separator')}{value}"
        return PROXY_COUNTRY_SELECTOR_RE.sub(replacement, text, count=1)
    return text


def _approve_exits(pool: Optional[list[str]], fallback: str, region: str, n: int) -> list[str]:
    """为 approve 挑 n 个「不同预置节点」的出口，全部强制成 channel 国家（IN/KR）。

    approve 会被 OpenAI 风控按出口 IP 逐个 block，所以必须走池里多条不同 sticky 线路
    并发打，命中一个 approved 即可。从随机位置开始遍历，避免多账号老是压同几行。
    """
    lines = [ln.strip() for ln in (pool or []) if ln and ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        lines = [fallback] if fallback else [""]
    n = max(1, min(int(n or 1), 16))
    start = random.randrange(len(lines)) if len(lines) > 1 else 0
    return [_force_region(lines[(start + i) % len(lines)], region) for i in range(n)]


def _normalize_proxy(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) >= 4:  # host:port:user:pass
        host, port = parts[0], parts[1]
        user, password = ":".join(parts[2:-1]), parts[-1]
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return "http://" + raw


def _kakao_proxy_chain(checkout_proxy: str, update_proxy: str) -> tuple[str, str]:
    """固定 pool1=KR checkout/provider、pool2=VN promotion 的职责。"""
    checkout = _force_region(checkout_proxy, METHODS["kakao"]["provider_country"])
    promotion_seed = update_proxy or checkout_proxy
    promotion = _force_region(promotion_seed, METHODS["kakao"]["promotion_country"])
    return checkout, promotion


def _proxy_dict(proxy: str) -> Optional[dict]:
    proxy = _normalize_proxy(proxy)
    return {"http": proxy, "https": proxy} if proxy else None


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        item = _normalize_proxy(item)
        if item and item not in seen:
            seen.append(item)
    return seen


def _oai_headers(token: str, device_id: str, path: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Oai-Language": "en-US",
        "oai-device-id": device_id,
        "Cookie": f"oai-did={device_id}",
    }
    if path:
        headers["X-OpenAI-Target-Path"] = path
        headers["X-OpenAI-Target-Route"] = path
    return headers


def _stripe_headers(accept_language: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": accept_language,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": UA,
    }


def _find(value, key: str):
    if isinstance(value, dict):
        got = value.get(key)
        if isinstance(got, str) and got:
            return got
        for nested in value.values():
            found = _find(nested, key)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find(nested, key)
            if found:
                return found
    return None


def _expected_amount(page: dict) -> str:
    for path in (("total_summary", "due"), ("invoice", "amount_due"),
                 ("elements_options", "amount"), ("total_summary", "total")):
        cur = page
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, int):
            return str(cur)
        if isinstance(cur, str) and cur.isdigit():
            return cur
    return "0"


def _redirect_url(details: dict) -> str:
    action = details.get("next_action") if isinstance(details, dict) else None
    if isinstance(action, dict):
        redirect = action.get("redirect_to_url") or {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    for key in ("setup_intent", "payment_intent"):
        node = details.get(key)
        if isinstance(node, dict):
            url = _redirect_url(node)
            if url:
                return url
    return ""


def _extract_link(details: dict, method: str) -> str:
    blob = json.dumps(details, ensure_ascii=False)
    urls = re.findall(r"https://(?:payments|qr|checkout|hooks)\.stripe\.com/[^\"\\ ]+", blob)
    redirect = _redirect_url(details)
    if method == "upi":
        match = METHODS["upi"]["link_match"]
        return next((u for u in urls if match in u), "") or (redirect if match in (redirect or "") else "")
    # Kakao 只接受 next_action.redirect_to_url；最终还会跟随并验证 Kakao/Nicepay 主机。
    return redirect


def _openai_return_url(cs_id: str, processor: str, hosted_url: str) -> str:
    if hosted_url.startswith("https://checkout.stripe.com/"):
        hosted_url = "https://pay.openai.com/" + hosted_url.split("https://checkout.stripe.com/", 1)[1]
    success = (
        "https://chatgpt.com/checkout/verify"
        f"?stripe_session_id={quote(cs_id, safe='')}"
        f"&processor_entity={quote(processor, safe='')}&plan_type=plus"
    )
    sep = "&" if "?" in hosted_url else "?"
    return hosted_url + sep + "success_return_url=" + quote(success, safe="")


def _require_ok(resp, stage: str) -> None:
    if 200 <= resp.status_code < 300:
        return
    hint = ""
    if resp.status_code in {401, 403}:
        hint = "（access token 失效或被 Cloudflare 拦截，建议先『刷新状态/取 RT』或换住宅代理）"
    raise RuntimeError(f"{stage} 失败 HTTP {resp.status_code}{hint}")


def _post_form(session, url, data, candidates, accept_language, timeout=90, headers=None):
    """按代理候选顺序 POST，全部失败才抛最后一个异常（对齐参考实现的多代理回退）。"""
    last: Optional[Exception] = None
    for proxy in candidates or [""]:
        try:
            return session.post(
                url, headers=headers or _stripe_headers(accept_language),
                data=data, proxies=_proxy_dict(proxy), timeout=timeout,
            )
        except Exception as exc:
            last = exc
    assert last is not None
    raise last


def _kakao_stripe_headers(publishable_key: str, referer: str, accept_language: str) -> dict[str, str]:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Accept": "application/json",
        "Accept-Language": accept_language,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": origin,
        "Referer": referer,
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": UA,
    }


def _kakao_elements_params(stripe_js_id: str, locale: str, session_id: str = "") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def _kakao_amount(page: dict) -> str:
    for path in (
        ("elements_options", "amount"),
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("invoice", "total"),
    ):
        cur = page
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, int):
            return str(cur)
        if isinstance(cur, str) and cur.isdigit():
            return cur
    return "unknown"


def _validate_kakao_checkout(page: dict, stage: str, *, require_zero: bool) -> str:
    amount = _kakao_amount(page)
    currency = str(page.get("currency") or "").lower()
    pm_types = {
        str(item).lower()
        for item in (page.get("payment_method_types") or []) + (page.get("ordered_payment_method_types") or [])
    }
    if "kakao_pay" not in pm_types or (require_zero and (amount != "0" or currency != "krw")):
        raise RuntimeError(
            "checkout_not_kakao_trial: "
            f"stage={stage} amount={amount} currency={currency or 'unknown'} "
            f"methods={sorted(pm_types)}"
        )
    return amount


def _ip_country(source: str, payload: dict) -> str:
    if source == "ipinfo":
        return str(payload.get("country") or "").upper()
    if source == "ipapi":
        return str(payload.get("country_code") or payload.get("country") or "").upper()
    if source == "ipwho":
        return str(payload.get("country_code") or "").upper() if payload.get("success") is not False else ""
    return str(payload.get("cc") or payload.get("country") or "").upper()


def _preflight_kakao_proxy(cffi, proxy: str, expected_country: str, label: str) -> None:
    if not proxy:
        raise RuntimeError(f"{label} 代理为空")
    session = cffi.Session(impersonate="chrome")
    failures: list[str] = []
    try:
        for source, url in KAKAO_IP_CHECK_SOURCES:
            try:
                response = session.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": UA},
                    proxies=_proxy_dict(proxy),
                    timeout=12,
                )
                if not (200 <= response.status_code < 300):
                    failures.append(f"{source}=HTTP {response.status_code}")
                    continue
                payload = response.json() or {}
                country = _ip_country(source, payload) if isinstance(payload, dict) else ""
                if not country:
                    failures.append(f"{source}=无国家")
                    continue
                if country != expected_country:
                    raise RuntimeError(f"{label} 代理出口国家 {country}，要求 {expected_country}")
                return
            except RuntimeError:
                raise
            except Exception as exc:
                failures.append(f"{source}={type(exc).__name__}")
    finally:
        try:
            session.close()
        except Exception:
            pass
    raise RuntimeError(f"{label} 代理出口预检失败（{'；'.join(failures[:4])}）")


def _activate_kakao_checkout(session, cs_id: str, proxy: str) -> str:
    checkout_page = f"https://checkout.stripe.com/c/pay/{cs_id}"
    for url in (f"https://pay.openai.com/c/pay/{cs_id}", checkout_page):
        session.get(
            url,
            headers={"Accept": "text/html,*/*", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8", "User-Agent": UA},
            proxies=_proxy_dict(proxy),
            timeout=30,
        )
    return checkout_page


def _update_kakao_checkout_taxes(
    session,
    token: str,
    device_id: str,
    cs_id: str,
    processor: str,
    profile: dict[str, str],
    proxy: str,
) -> None:
    path = "/backend-api/payments/checkout/taxes"
    headers = _oai_headers(token, device_id, path)
    headers.update({
        "Referer": f"https://chatgpt.com/checkout/{processor}/{cs_id}",
        "Accept-Language": METHODS["kakao"]["accept_language"],
        "Oai-Language": METHODS["kakao"]["locale"],
    })
    response = session.post(
        OAI_TAXES,
        headers=headers,
        json={
            "checkout_session_id": cs_id,
            "checkout_email": profile["email"],
            "billing_country": profile["country"],
            "billing_name": profile["name"],
            "currency": METHODS["kakao"]["currency"],
            "tax_id": None,
            "processor_entity": processor,
            "billing_address": {
                "line1": profile["line1"],
                "city": profile["city"],
                "country": profile["country"],
                "postal_code": profile["postal_code"],
                "state": profile["state"],
            },
        },
        proxies=_proxy_dict(proxy),
        timeout=90,
    )
    _require_ok(response, "openai_checkout_taxes")


def _is_https_domain(url: str, domains: tuple[str, ...]) -> bool:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _is_kakao_provider_url(url: str) -> bool:
    return _is_https_domain(url, KAKAO_PROVIDER_DOMAINS)


def _follow_kakao_redirect(session, url: str, proxy: str, *, max_hops: int = 6) -> str:
    current = str(url or "").strip()
    for _ in range(max_hops):
        if _is_kakao_provider_url(current):
            return current
        if not current:
            break
        # 只请求 Stripe 自身的中间跳转。尤其不能访问 confirm 中配置的
        # ChatGPT success return_url；本功能的边界是拿到 provider URL 即停止。
        if not _is_https_domain(current, KAKAO_REDIRECT_INTERMEDIATE_DOMAINS):
            raise RuntimeError("拒绝访问非 Stripe 的 Kakao 中间跳转（不会触发支付成功回调）")
        response = session.get(
            current,
            headers={"Accept": "text/html,*/*", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8", "User-Agent": UA},
            proxies=_proxy_dict(proxy),
            allow_redirects=False,
            timeout=30,
        )
        location = str((response.headers or {}).get("Location") or "")
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        current = urljoin(current, location)
    if _is_kakao_provider_url(current):
        return current
    raise RuntimeError("Kakao 跳转未落到 Kakao/Nicepay 域名")


def generate_link(
    access_token: str,
    method: str,
    *,
    checkout_proxy: str = "",
    update_proxy: str = "",
    checkout_pool: Optional[list[str]] = None,
    update_pool: Optional[list[str]] = None,
    poll_seconds: int = 35,
    approve_workers: int = 10,
    log: Optional[Callable[..., None]] = None,
) -> dict:
    """跑一遍提链流程，返回 {"ok", "link", "amount", "approve_states", ...}。

    UPI：IN checkout → BR promotion → IN confirm/approve/poll。
    Kakao：pool1 KR checkout/provider → pool2 VN promotion → pool1 KR
    taxes/pre_confirm/confirm/approve/poll → Kakao/Nicepay 最终跳转。

    checkout_proxy 走账单国出口（IN/KR），update_proxy 走压价出口（BR）。
    checkout_pool 是账单国整池，approve 从里面挑多条不同 sticky 线路并发打；
    留空则退化成只用 checkout_proxy 一条线。
    """
    method = (method or "").lower()
    if method not in METHODS:
        raise ValueError(f"不支持的支付方式: {method}")
    cfg = METHODS[method]
    is_kakao = method == "kakao"
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("该账号没有可用的网页 access token，请先『取 RT / 刷新状态』")
    emit = log or (lambda *a, **k: None)

    # UPI 保持原有 IN/BR 双池；Kakao 固定 pool1=KR provider、pool2=VN promotion。
    channel_region = cfg["country"]
    if is_kakao:
        checkout_proxy, update_proxy = _kakao_proxy_chain(checkout_proxy, update_proxy)
        poll_seconds = max(int(cfg.get("min_poll_seconds") or 0), int(poll_seconds or 0))
    else:
        checkout_proxy = _force_region(checkout_proxy, channel_region)

    cffi = _import_cffi()
    if is_kakao:
        emit("proxy_check", "校验 KR checkout/provider 与 VN promotion 出口", status="running")
        _preflight_kakao_proxy(cffi, checkout_proxy, cfg["provider_country"], "KR checkout/provider")
        _preflight_kakao_proxy(cffi, update_proxy, cfg["promotion_country"], "VN promotion")
    checkout_session = cffi.Session(impersonate="chrome")
    promotion_session = cffi.Session(impersonate="chrome") if is_kakao else checkout_session
    provider_session = cffi.Session(impersonate="chrome") if is_kakao else checkout_session
    approve_threads: list[threading.Thread] = []
    approved_flag = threading.Event()
    device_id = str(uuid.uuid4())
    profile = _profile(method)
    accept_language = cfg["accept_language"]
    elements_locale = str(cfg.get("elements_locale") or cfg["locale"])
    # Kakao 的 Stripe/provider 阶段严格留在 pool1 KR；UPI 保留原双池回退。
    stripe_candidates = ([checkout_proxy] if is_kakao else _unique([checkout_proxy, update_proxy])) or [""]

    try:
        emit("checkout", f"创建 {cfg['label']} checkout", status="running")
        r = checkout_session.post(
            OAI_CHECKOUT,
            headers=_oai_headers(token, device_id),
            json={
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": cfg["country"], "currency": cfg["currency"]},
                "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
                "checkout_ui_mode": "custom" if is_kakao else "hosted",
                **({"cancel_url": "https://chatgpt.com/#pricing"} if is_kakao else {}),
            },
            proxies=_proxy_dict(checkout_proxy),
            timeout=90,
        )
        _require_ok(r, "openai_checkout")
        checkout = r.json()
        cs_id = checkout["checkout_session_id"]
        pk = checkout["publishable_key"]
        processor = checkout.get("processor_entity") or "openai_llc"
        client_secret = checkout.get("client_secret") or ""
        hosted_url = f"https://checkout.stripe.com/c/pay/{cs_id}"
        if "_secret_" in client_secret:
            hosted_url += "#" + client_secret.split("_secret_", 1)[1]
        checkout_page = (
            _activate_kakao_checkout(checkout_session, cs_id, checkout_proxy)
            if is_kakao else hosted_url
        )

        client_session_id = str(uuid.uuid4())
        stripe_js_id = str(uuid.uuid4())
        init_form = {
            "browser_locale": cfg["locale"],
            "browser_timezone": cfg["timezone"],
            "redirect_type": "url",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": cfg["locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": STRIPE_VERSION,
        }
        if is_kakao:
            init_form.update({"eid": "NA", **_kakao_elements_params(stripe_js_id, elements_locale)})
        stripe_headers = (
            _kakao_stripe_headers(pk, checkout_page, accept_language)
            if is_kakao else _stripe_headers(accept_language)
        )
        emit("stripe_init", "初始化 Stripe 付款页", status="running")
        r = checkout_session.post(f"{STRIPE_API}/payment_pages/{cs_id}/init",
                         headers=stripe_headers, data=init_form,
                         proxies=_proxy_dict(checkout_proxy), timeout=90)
        _require_ok(r, "stripe_init")
        init = r.json()
        if is_kakao:
            _validate_kakao_checkout(init, "KR bootstrap", require_zero=False)
        elements_session_id = _find(init, "elements_session_id") or f"elements_session_{uuid.uuid4().hex[:6]}"

        emit("update", "应用促销 checkout/update", status="running")
        update_headers = _oai_headers(token, device_id, "/backend-api/payments/checkout/update")
        if is_kakao:
            update_headers.update({
                "Referer": f"https://chatgpt.com/checkout/{processor}/{cs_id}",
                "Accept-Language": accept_language,
                "Oai-Language": cfg["locale"],
            })
        r = promotion_session.post(
            OAI_UPDATE,
            headers=update_headers,
            json={
                "checkout_session_id": cs_id, "processor_entity": processor,
                "plan_name": "chatgptplusplan", "price_interval": "month", "seat_quantity": 1,
                "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
            },
            proxies=_proxy_dict(update_proxy), timeout=90,
        )
        _require_ok(r, "openai_checkout_update")
        if is_kakao:
            try:
                update_payload = r.json() or {}
            except (TypeError, ValueError):
                update_payload = {}
            if isinstance(update_payload, dict) and update_payload.get("success") is False:
                raise RuntimeError("openai_checkout_update 返回 success=false")

        r = provider_session.post(f"{STRIPE_API}/payment_pages/{cs_id}/init",
                         headers=stripe_headers, data=init_form,
                         proxies=_proxy_dict(checkout_proxy), timeout=90)
        _require_ok(r, "stripe_reinit")
        page = r.json()
        elements_session_id = _find(page, "elements_session_id") or elements_session_id
        if is_kakao:
            amount = _validate_kakao_checkout(page, "VN promotion 后 KR refresh", require_zero=True)
            emit("taxes", "同步 OpenAI checkout/taxes 与 Stripe tax_region", status="running")
            _update_kakao_checkout_taxes(
                provider_session, token, device_id, cs_id, processor, profile, checkout_proxy,
            )
        else:
            amount = _expected_amount(page)
            pm_types = (page.get("payment_method_types") or []) + (page.get("ordered_payment_method_types") or [])
            if cfg["pm_type"] not in pm_types:
                raise RuntimeError(f"该账号 checkout 未提供 {cfg['label']} 付款方式（pm_types={pm_types[:6]}）")
            if cfg["require_zero"] and amount != "0":
                raise RuntimeError(f"促销未生效，金额仍为 {amount}（需换 access token，不是换代理）")

        update_form = {
            "tax_region[country]": profile["country"], "tax_region[line1]": profile["line1"],
            "tax_region[city]": profile["city"], "tax_region[postal_code]": profile["postal_code"],
            "tax_region[state]": profile["state"],
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[stripe_js_id]": client_session_id,
            "elements_session_client[locale]": cfg["locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk, "_stripe_version": STRIPE_VERSION,
        }
        if is_kakao:
            update_form.update(_kakao_elements_params(stripe_js_id, elements_locale, elements_session_id))
        r = _post_form(provider_session, f"{STRIPE_API}/payment_pages/{cs_id}",
                       update_form, stripe_candidates, accept_language,
                       headers=stripe_headers if is_kakao else None)
        _require_ok(r, "stripe_update")
        page = r.json()
        if is_kakao:
            r = provider_session.post(
                f"{STRIPE_API}/payment_pages/{cs_id}/init",
                headers=stripe_headers,
                data=init_form,
                proxies=_proxy_dict(checkout_proxy),
                timeout=90,
            )
            _require_ok(r, "stripe_post_tax_reinit")
            page = r.json()
            elements_session_id = _find(page, "elements_session_id") or elements_session_id
            amount = _validate_kakao_checkout(page, "KR taxes 后 refresh", require_zero=True)
        else:
            amount = _expected_amount(page)
        config_id = _find(page, "config_id") or str(uuid.uuid4())

        if is_kakao:
            emit("pre_confirm", "激活 Kakao Pay pre_confirm", status="running")
            r = provider_session.post(
                f"{STRIPE_API}/payment_pages/{cs_id}/pre_confirm",
                headers=stripe_headers,
                data={
                    "eid": str(uuid.uuid4()),
                    "payment_method_type": cfg["pm_type"],
                    "key": pk,
                    "_stripe_version": STRIPE_VERSION,
                },
                proxies=_proxy_dict(checkout_proxy),
                timeout=90,
            )
            _require_ok(r, "stripe_pre_confirm")

        emit("confirm", "提交付款方式并确认", status="running")
        billing = {
            "billing_details[name]": profile["name"], "billing_details[email]": profile["email"],
            "billing_details[address][country]": profile["country"],
            "billing_details[address][line1]": profile["line1"],
            "billing_details[address][city]": profile["city"],
            "billing_details[address][postal_code]": profile["postal_code"],
            "billing_details[address][state]": profile["state"],
        }
        if is_kakao:
            billing["billing_details[address][line2]"] = str(profile.get("line2") or "")
        runtime_version = KAKAO_STRIPE_RUNTIME_VERSION if is_kakao else STRIPE_RUNTIME_VERSION
        pm_guid = str(uuid.uuid4())
        pm_muid = str(uuid.uuid4())
        pm_sid = str(uuid.uuid4())
        if is_kakao:
            # Kakao custom checkout 会在 payment_method 与 confirm 间复用同一组
            # Stripe 浏览器标识；尾部随机值与真实 Stripe.js 生成格式对齐。
            pm_guid += uuid.uuid4().hex[:6]
            pm_muid += uuid.uuid4().hex[:6]
            pm_sid += uuid.uuid4().hex[:6]
        pm_form = {
            **billing, "type": cfg["pm_type"],
            "payment_user_agent": (
                f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
                "payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com", "time_on_page": "31000",
            "client_attribution_metadata[client_session_id]": client_session_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[elements_session_id]": elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": config_id,
            "client_attribution_metadata[checkout_config_id]": config_id,
            "guid": pm_guid, "muid": pm_muid, "sid": pm_sid,
            "key": pk, "_stripe_version": STRIPE_VERSION,
        }
        if is_kakao:
            pm_form.update({
                "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; checkout",
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            })
            pm_form.pop("client_attribution_metadata[merchant_integration_subtype]", None)
            pm_form.pop("client_attribution_metadata[payment_intent_creation_flow]", None)
        r = _post_form(provider_session, f"{STRIPE_API}/payment_methods",
                       pm_form, stripe_candidates, accept_language,
                       headers=stripe_headers if is_kakao else None)
        _require_ok(r, "stripe_payment_method")
        pm_id = r.json()["id"]
        if is_kakao and not str(pm_id).startswith("pm_"):
            raise RuntimeError("Kakao payment_method 未返回有效 pm_ id")

        if is_kakao:
            success_url = (
                f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{cs_id}/success"
                f"?billing_country={cfg['provider_country']}"
            )
            confirm_return_url = (
                f"https://checkout.stripe.com/c/pay/{cs_id}?returned_from_redirect=true&ui_mode=custom&"
                f"return_url={quote(success_url, safe='')}"
            )
        else:
            confirm_return_url = _openai_return_url(
                cs_id, processor, _find(page, "stripe_hosted_url") or hosted_url,
            )
        confirm_form = {
            "guid": pm_guid if is_kakao else str(uuid.uuid4()),
            "muid": pm_muid if is_kakao else str(uuid.uuid4()),
            "sid": pm_sid if is_kakao else str(uuid.uuid4()),
            "payment_method": pm_id,
            "init_checksum": _find(page, "init_checksum") or _find(init, "init_checksum") or "",
            "version": runtime_version, "expected_amount": amount,
            "expected_payment_method_type": cfg["pm_type"],
            "return_url": confirm_return_url,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[stripe_js_id]": client_session_id,
            "elements_session_client[locale]": cfg["locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "client_attribution_metadata[client_session_id]": client_session_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[elements_session_id]": elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": config_id,
            "client_attribution_metadata[checkout_config_id]": config_id,
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "consent[terms_of_service]": "accepted",
            "key": pk, "_stripe_version": STRIPE_VERSION,
        }
        if is_kakao:
            confirm_form.update({
                **_kakao_elements_params(stripe_js_id, elements_locale, elements_session_id),
                "eid": "NA",
                "tax_id_collection[purchasing_as_business]": "false",
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
                "link_brand": "link",
            })
            confirm_form.pop("client_attribution_metadata[merchant_integration_subtype]", None)
            confirm_form.pop("client_attribution_metadata[payment_intent_creation_flow]", None)
        r = _post_form(provider_session, f"{STRIPE_API}/payment_pages/{cs_id}/confirm",
                       confirm_form, stripe_candidates, accept_language,
                       headers=stripe_headers if is_kakao else None)
        _require_ok(r, "stripe_confirm")
        confirm_payload = r.json() if is_kakao else {}

        # UPI 保持原来始终并发 approve；Kakao 仅在 confirm 明确要求人工审批且尚无
        # redirect 时审批。两者都保留 pool1 多出口并发命中能力。
        stripe_redirect = _extract_link(confirm_payload, method) if is_kakao else ""
        submission = (
            confirm_payload.get("submission_attempt")
            if isinstance(confirm_payload.get("submission_attempt"), dict)
            else {}
        )
        should_approve = not is_kakao or (
            not stripe_redirect
            and (
                submission.get("state") == "requires_approval"
                or bool(checkout.get("requires_manual_approval"))
            )
        )
        approve_states: list[str] = []
        states_lock = threading.Lock()

        def approve(proxy: str) -> None:
            # 用独立 session 避免共享连接的线程安全问题。
            if approved_flag.is_set():
                return
            sess = None
            state = "error"
            try:
                if is_kakao:
                    _preflight_kakao_proxy(
                        cffi, proxy, cfg["provider_country"], "KR approve",
                    )
                if approved_flag.is_set():
                    return
                sess = cffi.Session(impersonate="chrome")
                if approved_flag.is_set():
                    return
                resp = sess.post(
                    OAI_APPROVE,
                    headers={**_oai_headers(token, device_id, "/backend-api/payments/checkout/approve"),
                             "Referer": f"https://chatgpt.com/checkout/{processor}/{cs_id}",
                             "Accept-Language": accept_language,
                             "Oai-Language": cfg["locale"]},
                    json={"checkout_session_id": cs_id, "processor_entity": processor},
                    proxies=_proxy_dict(proxy), timeout=18,
                )
                try:
                    state = str((resp.json() or {}).get("result") or resp.status_code).lower()
                except Exception:
                    state = str(resp.status_code)
            except Exception as exc:
                state = "timeout" if "timeout" in type(exc).__name__.lower() else "error"
            finally:
                try:
                    if sess is not None:
                        sess.close()
                except Exception:
                    pass
            with states_lock:
                approve_states.append(state)
            if state == "approved":
                approved_flag.set()

        if should_approve:
            exits = _approve_exits(checkout_pool, checkout_proxy, channel_region, approve_workers)
            emit("approve", f"OpenAI 审批（{len(exits)} 路 {channel_region} 出口并发）", status="running")
            for i, exit_proxy in enumerate(exits):
                if approved_flag.is_set():
                    break
                t = threading.Thread(target=approve, args=(exit_proxy,), daemon=True)
                t.start()
                approve_threads.append(t)
                time.sleep(0.05 if i < 4 else 0.08)

        link = stripe_redirect
        poll_proxy = _force_region(checkout_proxy, channel_region)
        deadline = time.time() + max(5, poll_seconds)
        details_params_key = client_session_id
        details_params = {
            "key": pk, "_stripe_version": STRIPE_VERSION,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[stripe_js_id]": details_params_key,
            "elements_session_client[locale]": cfg["locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        }
        if is_kakao:
            details_params = {
                "key": pk,
                "_stripe_version": STRIPE_VERSION,
                **_kakao_elements_params(stripe_js_id, elements_locale, elements_session_id),
        }
        if is_kakao and not link:
            emit("poll", f"轮询 Stripe redirect（最长 {poll_seconds} 秒）", status="running")
        while not link and time.time() < deadline:
            try:
                remaining = max(1.0, deadline - time.time())
                r = provider_session.get(
                    f"{STRIPE_API}/payment_pages/{cs_id}",
                    headers=stripe_headers if is_kakao else _stripe_headers(accept_language),
                    params=details_params,
                    proxies=_proxy_dict(poll_proxy),
                    timeout=min(8.0 if is_kakao else 30.0, remaining),
                )
                if r.ok:
                    link = _extract_link(r.json(), method)
                    if link:
                        break
            except Exception as exc:
                if not is_kakao:
                    raise
                emit(
                    "poll",
                    f"Stripe 轮询暂时失败（{type(exc).__name__}），将在总窗口内重试",
                    status="warning",
                )
            time.sleep(1)

        # provider URL 已出现（或轮询到期）后，不允许 approve 线程在函数返回后
        # 继续发请求；先阻止尚未开始的请求，再回收所有有界超时线程。
        approved_flag.set()
        for t in approve_threads:
            t.join()

        stripe_redirect_url = link
        if is_kakao and link:
            emit("redirect", "跟随 Kakao/Nicepay 最终跳转", status="running")
            link = _follow_kakao_redirect(provider_session, link, poll_proxy)

        return {
            "ok": bool(link),
            "link": link,
            "method": method,
            "amount": amount,
            "checkout_session_id": cs_id,
            "approve_states": list(approve_states),
            "stripe_redirect_url": stripe_redirect_url if is_kakao else "",
            "provider_redirect_url": link if is_kakao else "",
        }
    finally:
        approved_flag.set()
        for approve_thread in approve_threads:
            if approve_thread is not threading.current_thread():
                approve_thread.join()
        closed: set[int] = set()
        for active_session in (checkout_session, promotion_session, provider_session):
            if id(active_session) in closed:
                continue
            closed.add(id(active_session))
            try:
                active_session.close()
            except Exception:
                pass
