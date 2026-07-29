"""账号管理页的手动 Codex OAuth PKCE 授权与 Sub2 文件生成。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
FLOW_TTL_SECONDS = 10 * 60


class ManualOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Flow:
    email: str
    state: str
    verifier: str
    created_at: float


_flows: dict[str, _Flow] = {}
_lock = threading.Lock()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt_payload(token: str) -> dict:
    try:
        part = str(token or "").split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _prune_locked(now: float) -> None:
    expired = [
        flow_id for flow_id, flow in _flows.items()
        if now - flow.created_at > FLOW_TTL_SECONDS
    ]
    for flow_id in expired:
        _flows.pop(flow_id, None)


def start_flow(email: str) -> dict:
    account_email = str(email or "").strip().lower()
    if not account_email:
        raise ManualOAuthError("账号邮箱不能为空")
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    oauth_state = secrets.token_urlsafe(24)
    flow_id = secrets.token_urlsafe(24)
    now = time.time()
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": oauth_state,
    }
    with _lock:
        _prune_locked(now)
        _flows[flow_id] = _Flow(account_email, oauth_state, verifier, now)
    return {
        "flow_id": flow_id,
        "email": account_email,
        "auth_url": f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        "redirect_uri": REDIRECT_URI,
        "expires_at": now + FLOW_TTL_SECONDS,
    }


def _exchange_code(code: str, verifier: str) -> dict:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise ManualOAuthError("服务器缺少 curl_cffi，无法交换 OAuth Token") from exc
    try:
        response = requests.post(
            TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Origin": "https://auth.openai.com",
                "Referer": "https://auth.openai.com/",
            },
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            impersonate="chrome136",
            timeout=30,
        )
    except Exception as exc:
        raise ManualOAuthError("OpenAI OAuth Token 交换网络失败") from exc
    if int(response.status_code) != 200:
        raise ManualOAuthError(f"OpenAI OAuth Token 交换失败：HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise ManualOAuthError("OpenAI OAuth Token 返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise ManualOAuthError("OpenAI OAuth Token 返回格式错误")
    return payload


def complete_flow(
    flow_id: str,
    callback_url: str,
    *,
    exchanger: Callable[[str, str], dict] | None = None,
) -> dict:
    flow_key = str(flow_id or "").strip()
    callback = str(callback_url or "").strip()
    with _lock:
        now = time.time()
        _prune_locked(now)
        flow = _flows.get(flow_key)
    if not flow:
        raise ManualOAuthError("手动授权已过期，请重新点击“手动 Sub2”")

    parsed = urllib.parse.urlsplit(callback)
    if (
        parsed.scheme != "http"
        or (parsed.hostname or "").lower() != "localhost"
        or parsed.port != 1455
        or parsed.path.rstrip("/") != "/auth/callback"
    ):
        raise ManualOAuthError("请粘贴完整的 localhost:1455/auth/callback 回调 URL")
    query = urllib.parse.parse_qs(parsed.query)
    oauth_error = str((query.get("error") or [""])[0] or "").strip()
    if oauth_error:
        raise ManualOAuthError(f"OpenAI 授权未完成：{oauth_error}")
    code = str((query.get("code") or [""])[0] or "").strip()
    got_state = str((query.get("state") or [""])[0] or "").strip()
    if not code:
        raise ManualOAuthError("回调 URL 中没有 code")
    if not got_state or not secrets.compare_digest(got_state, flow.state):
        raise ManualOAuthError("回调 state 不匹配，请使用本次授权生成的回调 URL")

    # code 只能使用一次；开始交换前即移除流程，避免重复提交。
    with _lock:
        _flows.pop(flow_key, None)
    tokens = (exchanger or _exchange_code)(code, flow.verifier)
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    if not access_token or not refresh_token:
        raise ManualOAuthError("OAuth 授权成功，但返回中缺少 access_token 或 refresh_token")

    claims = _jwt_payload(access_token) or _jwt_payload(id_token)
    profile = claims.get("https://api.openai.com/profile") or {}
    auth = claims.get("https://api.openai.com/auth") or {}
    authorized_email = str(
        (profile.get("email") if isinstance(profile, dict) else "")
        or claims.get("email")
        or flow.email
    ).strip().lower()
    if authorized_email != flow.email:
        raise ManualOAuthError(
            f"授权账号与所选账号不一致：请选择 {flow.email} 登录后重新授权"
        )
    account_id = str(
        (auth.get("chatgpt_account_id") if isinstance(auth, dict) else "")
        or (auth.get("account_id") if isinstance(auth, dict) else "")
        or ""
    ).strip()
    sub2 = {
        "version": 1,
        "accounts": [{
            "name": authorized_email,
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "refresh_token": refresh_token,
                "access_token": access_token,
                "chatgpt_account_id": account_id,
            },
        }],
        "proxies": [],
    }
    content = json.dumps(sub2, indent=2, ensure_ascii=False).encode("utf-8")
    safe_email = re.sub(r"[^A-Za-z0-9._-]+", "_", authorized_email.replace("@", "_at_"))
    return {
        "email": authorized_email,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "filename": f"sub2_{safe_email[:120] or 'account'}.json",
        "content": content,
    }


def _reset_for_tests() -> None:
    with _lock:
        _flows.clear()
