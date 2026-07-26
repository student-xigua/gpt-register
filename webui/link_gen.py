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
from urllib.parse import quote

from .exporter import _import_cffi

OAI_CHECKOUT = "https://chatgpt.com/backend-api/payments/checkout"
OAI_UPDATE = "https://chatgpt.com/backend-api/payments/checkout/update"
OAI_APPROVE = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME_VERSION = "6f8494a281"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
PROMO_ID = "plus-1-month-free"

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
        "timezone": "Asia/Seoul",
        "accept_language": "ko-KR,ko;q=0.9,en;q=0.8",
        "pm_type": "kakao_pay",
        # kakao_pay 是跳转式付款，链接来自 next_action.redirect_to_url，不强制 0 元。
        "require_zero": False,
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
    for key in ("setup_intent", "payment_intent"):
        node = details.get(key)
        if isinstance(node, dict):
            url = (((node.get("next_action") or {}).get("redirect_to_url") or {}).get("url"))
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
    # kakao_pay：跳转链接优先取 redirect_to_url
    return redirect or next((u for u in urls if "kakao" in u.lower()), "")


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


def _post_form(session, url, data, candidates, accept_language, timeout=90):
    """按代理候选顺序 POST，全部失败才抛最后一个异常（对齐参考实现的多代理回退）。"""
    last: Optional[Exception] = None
    for proxy in candidates or [""]:
        try:
            return session.post(
                url, headers=_stripe_headers(accept_language),
                data=data, proxies=_proxy_dict(proxy), timeout=timeout,
            )
        except Exception as exc:
            last = exc
    assert last is not None
    raise last


def generate_link(
    access_token: str,
    method: str,
    *,
    checkout_proxy: str = "",
    update_proxy: str = "",
    poll_seconds: int = 35,
    approve_workers: int = 2,
    log: Optional[Callable[..., None]] = None,
) -> dict:
    """跑一遍提链流程，返回 {"ok", "link", "amount", ...}。

    checkout_proxy 走账单国出口（IN/KR），update_proxy 走压价出口；
    只给一个代理时两者相同即可。
    """
    method = (method or "").lower()
    if method not in METHODS:
        raise ValueError(f"不支持的支付方式: {method}")
    cfg = METHODS[method]
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("该账号没有可用的网页 access token，请先『取 RT / 刷新状态』")
    emit = log or (lambda *a, **k: None)

    cffi = _import_cffi()
    session = cffi.Session(impersonate="chrome")
    device_id = str(uuid.uuid4())
    profile = _profile(method)
    accept_language = cfg["accept_language"]
    stripe_candidates = _unique([update_proxy, checkout_proxy]) or [""]

    try:
        emit("checkout", f"创建 {cfg['label']} checkout", status="running")
        r = session.post(
            OAI_CHECKOUT,
            headers=_oai_headers(token, device_id),
            json={
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": cfg["country"], "currency": cfg["currency"]},
                "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
                "checkout_ui_mode": "hosted",
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
        emit("stripe_init", "初始化 Stripe 付款页", status="running")
        r = session.post(f"{STRIPE_API}/payment_pages/{cs_id}/init",
                         headers=_stripe_headers(accept_language), data=init_form,
                         proxies=_proxy_dict(checkout_proxy), timeout=90)
        _require_ok(r, "stripe_init")
        init = r.json()
        elements_session_id = _find(init, "elements_session_id") or f"elements_session_{uuid.uuid4().hex[:6]}"

        emit("update", "应用促销 checkout/update", status="running")
        r = session.post(
            OAI_UPDATE,
            headers=_oai_headers(token, device_id, "/backend-api/payments/checkout/update"),
            json={
                "checkout_session_id": cs_id, "processor_entity": processor,
                "plan_name": "chatgptplusplan", "price_interval": "month", "seat_quantity": 1,
                "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
            },
            proxies=_proxy_dict(update_proxy), timeout=90,
        )
        _require_ok(r, "openai_checkout_update")

        r = session.post(f"{STRIPE_API}/payment_pages/{cs_id}/init",
                         headers=_stripe_headers(accept_language), data=init_form,
                         proxies=_proxy_dict(checkout_proxy), timeout=90)
        _require_ok(r, "stripe_reinit")
        page = r.json()
        elements_session_id = _find(page, "elements_session_id") or elements_session_id
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
        r = _post_form(session, f"{STRIPE_API}/payment_pages/{cs_id}",
                       update_form, stripe_candidates, accept_language)
        _require_ok(r, "stripe_update")
        page = r.json()
        amount = _expected_amount(page)
        config_id = _find(page, "config_id") or str(uuid.uuid4())

        emit("confirm", "提交付款方式并确认", status="running")
        billing = {
            "billing_details[name]": profile["name"], "billing_details[email]": profile["email"],
            "billing_details[address][country]": profile["country"],
            "billing_details[address][line1]": profile["line1"],
            "billing_details[address][city]": profile["city"],
            "billing_details[address][postal_code]": profile["postal_code"],
            "billing_details[address][state]": profile["state"],
        }
        pm_form = {
            **billing, "type": cfg["pm_type"],
            "payment_user_agent": (
                f"stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; "
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
            "guid": str(uuid.uuid4()), "muid": str(uuid.uuid4()), "sid": str(uuid.uuid4()),
            "key": pk, "_stripe_version": STRIPE_VERSION,
        }
        r = _post_form(session, f"{STRIPE_API}/payment_methods",
                       pm_form, stripe_candidates, accept_language)
        _require_ok(r, "stripe_payment_method")
        pm_id = r.json()["id"]

        confirm_form = {
            "guid": str(uuid.uuid4()), "muid": str(uuid.uuid4()), "sid": str(uuid.uuid4()),
            "payment_method": pm_id,
            "init_checksum": _find(page, "init_checksum") or _find(init, "init_checksum") or "",
            "version": STRIPE_RUNTIME_VERSION, "expected_amount": amount,
            "expected_payment_method_type": cfg["pm_type"],
            "return_url": _openai_return_url(cs_id, processor, _find(page, "stripe_hosted_url") or hosted_url),
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
        r = _post_form(session, f"{STRIPE_API}/payment_pages/{cs_id}/confirm",
                       confirm_form, stripe_candidates, accept_language)
        _require_ok(r, "stripe_confirm")

        # approve 与 details 轮询并行：一条路被 blocked，另一条出口可能 approved。
        emit("approve", "OpenAI 审批并轮询提取链接", status="running")
        approve_routes = _unique([update_proxy, checkout_proxy]) or [""]

        def approve(proxy: str) -> None:
            # approve 与主线程的 details 轮询并行，用独立 session 避免共享连接的线程安全问题。
            sess = cffi.Session(impersonate="chrome")
            try:
                sess.post(
                    OAI_APPROVE,
                    headers={**_oai_headers(token, device_id, "/backend-api/payments/checkout/approve"),
                             "Referer": f"https://chatgpt.com/checkout/{processor}/{cs_id}"},
                    json={"checkout_session_id": cs_id, "processor_entity": processor},
                    proxies=_proxy_dict(proxy), timeout=40,
                )
            except Exception:
                pass
            finally:
                try:
                    sess.close()
                except Exception:
                    pass

        threads = []
        for i in range(max(1, approve_workers)):
            t = threading.Thread(target=approve, args=(approve_routes[i % len(approve_routes)],), daemon=True)
            threads.append(t)
            t.start()

        link = ""
        deadline = time.time() + max(5, poll_seconds)
        details_params_key = client_session_id
        while time.time() < deadline:
            for proxy in _unique([update_proxy, checkout_proxy]) or [""]:
                r = session.get(
                    f"{STRIPE_API}/payment_pages/{cs_id}",
                    headers=_stripe_headers(accept_language),
                    params={
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
                    },
                    proxies=_proxy_dict(proxy), timeout=30,
                )
                if r.ok:
                    link = _extract_link(r.json(), method)
                    if link:
                        break
            if link:
                break
            time.sleep(1)

        for t in threads:
            t.join(timeout=1)

        return {
            "ok": bool(link),
            "link": link,
            "method": method,
            "amount": amount,
            "checkout_session_id": cs_id,
        }
    finally:
        try:
            session.close()
        except Exception:
            pass
