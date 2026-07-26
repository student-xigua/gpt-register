"""ChatGPT 2FA(TOTP) 绑定的协议原语。

对应网页端「设置 → 安全 → 二步验证」的四步：
    1. 重认证：signin/openai?reauth=password 触发邮箱 OTP，验证后换一份
       pwd_auth_time 新鲜的网页 accessToken；
    2. enroll：backend-api/accounts/mfa/enroll 拿 secret + session_id；
    3. activate：用 secret 现算 6 位码激活 enrollment；
    4. 保存 secret（OpenAI 只在 enroll 时返回一次，丢了无法找回）。

TOTP 用标准库实现（RFC 6238，SHA1/30s/6 位），不额外引入依赖。
编排与错误分级在 account_ops 里，本模块只做协议与计算。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import struct
import time
from urllib.parse import urlencode

logger = logging.getLogger("webui.twofa")

CHATGPT_ORIGIN = "https://chatgpt.com"
SESSION_URL = f"{CHATGPT_ORIGIN}/api/auth/session"
SIGNIN_URL = f"{CHATGPT_ORIGIN}/api/auth/signin/openai"
ENROLL_URL = f"{CHATGPT_ORIGIN}/backend-api/accounts/mfa/enroll"
ACTIVATE_URL = f"{CHATGPT_ORIGIN}/backend-api/accounts/mfa/user/activate_enrollment"
OTP_VALIDATE_URL = "https://auth.openai.com/api/accounts/email-otp/validate"
REAUTH_CALLBACK = f"{CHATGPT_ORIGIN}/?action=enable&factor=totp"


class TwoFactorProtocolError(RuntimeError):
    """2FA 协议步骤失败；status 用于让调用方区分「登录态过期」和「真失败」。"""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = int(status or 0)


# ──────────────────────── TOTP 计算 ────────────────────────


def normalize_secret(secret: str) -> str:
    """去掉空格/横线并转大写，得到可用于 base32 解码的密钥。"""
    return "".join(str(secret or "").split()).replace("-", "").upper()


def totp_now(secret: str, *, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    """按 RFC 6238 算当前 6 位动态码。"""
    cleaned = normalize_secret(secret)
    if not cleaned:
        raise ValueError("2FA 密钥为空")
    try:
        key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8), casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("2FA 密钥不是合法的 Base32 字符串") from exc
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ──────────────────────── 会话辅助 ────────────────────────


def _navigate_headers(flow, referer: str) -> dict:
    """浏览器跳转用的头；OAuth 重定向链对 Accept/Sec-Fetch 敏感。"""
    headers = flow._common_headers(referer)
    headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    )
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    headers["Sec-Fetch-Site"] = "same-origin"
    return headers


def _backend_headers(flow, access_token: str) -> dict:
    headers = flow._common_headers(f"{CHATGPT_ORIGIN}/")
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {access_token}"
    headers["oai-language"] = "zh-CN"
    device_id = device_id_of(flow)
    if device_id:
        headers["oai-device-id"] = device_id
    return headers


def device_id_of(flow) -> str:
    device_id = str(getattr(flow.result, "device_id", "") or "").strip()
    if device_id:
        return device_id
    try:
        return str(flow.session.cookies.get("oai-did", "") or "").strip()
    except Exception:
        return ""


def _json_body(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def fetch_web_session(flow) -> dict:
    """GET /api/auth/session，返回 {access_token, mfa, email}。"""
    resp = flow.session.get(
        SESSION_URL,
        headers=flow._common_headers(f"{CHATGPT_ORIGIN}/"),
        timeout=30,
    )
    if resp.status_code != 200:
        raise TwoFactorProtocolError(
            f"读取网页会话失败 (HTTP {resp.status_code})", status=resp.status_code
        )
    data = _json_body(resp)
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {
        "access_token": str(data.get("accessToken") or "").strip(),
        "session_token": str(flow.session.cookies.get("__Secure-next-auth.session-token", "") or ""),
        "mfa": bool(user.get("mfa")),
        "email": str(user.get("email") or "").strip(),
    }


# ──────────────────────── 重认证（触发邮箱 OTP） ────────────────────────


def trigger_reauth(flow, email: str) -> str:
    """POST signin/openai?reauth=password，返回 authorize URL。"""
    csrf_token = flow.get_csrf_token()
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
    }
    device_id = device_id_of(flow)
    if device_id:
        query["ext-oai-did"] = device_id
    headers = flow._common_headers(f"{CHATGPT_ORIGIN}/")
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = flow.session.post(
        f"{SIGNIN_URL}?{urlencode(query)}",
        headers=headers,
        data={
            "callbackUrl": REAUTH_CALLBACK,
            "csrfToken": csrf_token,
            "json": "true",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise TwoFactorProtocolError(
            f"发起重认证失败 (HTTP {resp.status_code})", status=resp.status_code
        )
    auth_url = str(_json_body(resp).get("url") or "").strip()
    if not auth_url:
        raise TwoFactorProtocolError("发起重认证失败：响应没有授权地址")
    return auth_url


def follow_reauth(flow, auth_url: str) -> str:
    """跟随 authorize 链；OpenAI 在这一步把重认证验证码发到邮箱。"""
    resp = flow.session.get(
        auth_url,
        headers=_navigate_headers(flow, f"{CHATGPT_ORIGIN}/"),
        timeout=45,
        allow_redirects=True,
    )
    return str(getattr(resp, "url", "") or "")


def validate_reauth_otp(flow, code: str) -> str:
    """提交邮箱验证码，返回回跳 chatgpt.com 的 continue_url。"""
    headers = flow._common_headers("https://auth.openai.com/email-verification")
    headers["Content-Type"] = "application/json"
    resp = flow.session.post(
        OTP_VALIDATE_URL,
        headers=headers,
        data=json.dumps({"code": str(code).strip()}),
        timeout=30,
    )
    if resp.status_code != 200:
        raise TwoFactorProtocolError(
            f"重认证验证码校验失败 (HTTP {resp.status_code})", status=resp.status_code
        )
    continue_url = str(_json_body(resp).get("continue_url") or "").strip()
    if not continue_url:
        raise TwoFactorProtocolError("重认证验证码校验失败：响应没有 continue_url")
    return continue_url


def exchange_web_session(flow, continue_url: str) -> dict:
    """跟随 continue_url 刷新 session cookie，再取一份新鲜的网页会话。"""
    flow.session.get(
        continue_url,
        headers=_navigate_headers(flow, "https://auth.openai.com/email-verification"),
        timeout=45,
        allow_redirects=True,
    )
    session = fetch_web_session(flow)
    if not session["access_token"]:
        raise TwoFactorProtocolError("重认证完成但没有拿到新的网页 AT")
    return session


# ──────────────────────── enroll / activate ────────────────────────


def enroll_totp(flow, access_token: str) -> tuple[str, str]:
    """注册 TOTP，返回 (secret, session_id)。"""
    resp = flow.session.post(
        ENROLL_URL,
        headers=_backend_headers(flow, access_token),
        data=json.dumps({"factor_type": "totp"}),
        timeout=30,
    )
    if resp.status_code != 200:
        raise TwoFactorProtocolError(
            f"注册 TOTP 失败 (HTTP {resp.status_code})", status=resp.status_code
        )
    data = _json_body(resp)
    secret = str(data.get("secret") or "").strip()
    session_id = str(data.get("session_id") or "").strip()
    if not secret or not session_id:
        raise TwoFactorProtocolError("注册 TOTP 失败：响应缺少 secret 或 session_id")
    return secret, session_id


def activate_totp(flow, access_token: str, secret: str, session_id: str) -> None:
    """用 secret 现算动态码激活 enrollment。"""
    resp = flow.session.post(
        ACTIVATE_URL,
        headers=_backend_headers(flow, access_token),
        data=json.dumps({
            "code": totp_now(secret),
            "factor_type": "totp",
            "session_id": session_id,
        }),
        timeout=30,
    )
    if resp.status_code != 200:
        raise TwoFactorProtocolError(
            f"激活 2FA 失败 (HTTP {resp.status_code})", status=resp.status_code
        )
    if not _json_body(resp).get("success"):
        raise TwoFactorProtocolError("激活 2FA 失败：接口未返回 success")


# ──────────────────────── 交付格式 ────────────────────────


def copy_line(email: str, password: str, secret: str) -> str:
    """账号管理页复制用的整行：账号----密码----2FA 密钥。"""
    return "----".join([
        str(email or "").strip(),
        str(password or "").strip(),
        normalize_secret(secret),
    ])
