"""账号管理后台任务：已有账号取 RT、状态刷新与 Sub2API 文件生成。

任务状态和下载内容只保存在当前进程内存中；下载完成即移除文件内容，
不会把含 token 的导出文件长期写入服务器磁盘。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from auth_flow import AuthFlow
from config import Config
from mail_outlook import OutlookMailProvider

from . import db, exporter

logger = logging.getLogger("webui.account_ops")
TASK_TTL_SECONDS = 3600


@dataclass
class AccountTask:
    task_id: str
    kind: str
    total: int
    state: str = "queued"
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    message: str = "等待执行"
    current_email: str = ""
    errors: list[dict] = field(default_factory=list)
    results: dict = field(default_factory=dict)
    artifact: bytes | None = None
    filename: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def public(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "state": self.state,
            "total": self.total,
            "completed": self.completed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "message": self.message,
            "current_email": self.current_email,
            "errors": list(self.errors),
            "results": dict(self.results),
            "download_ready": bool(self.artifact and self.filename),
            "download_url": (
                f"/api/account-tasks/{self.task_id}/download"
                if self.artifact and self.filename else ""
            ),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


_tasks: dict[str, AccountTask] = {}
_lock = threading.Lock()


def _clean_email(value: str) -> str:
    return str(value or "").strip().lower()


def _safe_error(exc: Exception) -> str:
    """返回可展示错误，避免把 token/API key 跟随上游响应写进日志或任务状态。"""
    text = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
    lowered = text.lower()
    sensitive_markers = (
        "access_token", "refresh_token", "session_token", "authorization",
        "api key", "api_key", "bearer ",
    )
    if any(marker in lowered for marker in sensitive_markers):
        return f"{exc.__class__.__name__}（敏感响应已隐藏）"
    text = re.sub(r"\beyJ[A-Za-z0-9_.-]{24,}\b", "[token hidden]", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{80,}\b", "[secret hidden]", text)
    return text[:180] or exc.__class__.__name__


def _safe_filename_email(email: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(email or "").replace("@", "_at_"))
    return cleaned[:120] or "account"


def _new_task(kind: str, total: int) -> AccountTask:
    task = AccountTask(task_id=uuid.uuid4().hex, kind=kind, total=total)
    with _lock:
        now = time.time()
        expired = [
            key for key, item in _tasks.items()
            if item.finished_at and now - item.finished_at > TASK_TTL_SECONDS
        ]
        for key in expired:
            _tasks.pop(key, None)
        _tasks[task.task_id] = task
    return task


def get_task(task_id: str) -> Optional[dict]:
    with _lock:
        task = _tasks.get(task_id)
        return task.public() if task else None


def pop_artifact(task_id: str) -> Optional[tuple[bytes, str]]:
    with _lock:
        task = _tasks.get(task_id)
        if not task or not task.artifact or not task.filename:
            return None
        artifact = task.artifact
        filename = task.filename
        task.artifact = None
        task.filename = ""
        return artifact, filename


def _run_async(task: AccountTask, worker: Callable[[AccountTask], None]) -> str:
    def run() -> None:
        task.state = "running"
        try:
            worker(task)
            task.state = "done" if task.failed == 0 else (
                "partial" if task.succeeded else "failed"
            )
            if not task.message or task.message == "处理中":
                task.message = "任务完成"
        except Exception as exc:
            task.state = "failed"
            task.failed += 1
            task.message = _safe_error(exc)
            logger.warning("账号任务 %s 失败: %s", task.task_id, task.message)
        finally:
            task.current_email = ""
            task.finished_at = time.time()

    threading.Thread(
        target=run,
        daemon=True,
        name=f"account-task-{task.task_id[:8]}",
    ).start()
    return task.task_id


def _sub2_account(cred: dict, fresh: dict) -> dict:
    access_token = str(fresh.get("access_token") or "").strip()
    refresh_token = str(
        fresh.get("refresh_token") or cred.get("refresh_token") or ""
    ).strip()
    payload = exporter._decode_jwt_payload(access_token)
    profile = exporter._get_profile(payload)
    auth = exporter._get_auth(payload)
    email = str(profile.get("email") or payload.get("email") or cred.get("email") or "").strip()
    account_id = str(
        auth.get("chatgpt_account_id") or auth.get("account_id") or ""
    ).strip()
    if not access_token or not refresh_token:
        raise RuntimeError("刷新结果缺少必要凭证")
    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "refresh_token": refresh_token,
            "access_token": access_token,
            "chatgpt_account_id": account_id,
        },
    }


def _build_artifact(accounts: list[dict]) -> bytes:
    return json.dumps(
        {"version": 1, "accounts": accounts, "proxies": []},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def start_sub2_export(emails: list[str]) -> tuple[str, int, int]:
    """后台刷新有 RT 的账号并生成一个合并 JSON；无 RT 的账号在启动前过滤。"""
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    eligible: list[dict] = []
    for email in requested:
        cred = db.get_registered(email)
        if cred and str(cred.get("refresh_token") or "").strip():
            eligible.append(cred)
    task = _new_task("sub2_export", len(eligible))
    task.skipped = len(requested) - len(eligible)

    def worker(state: AccountTask) -> None:
        accounts: list[dict] = []
        for cred in eligible:
            email = cred["email"]
            state.current_email = email
            state.message = f"正在刷新 {state.completed + 1}/{state.total}"
            try:
                fresh = exporter.refresh_codex_token(cred["refresh_token"])
                rolled_rt = str(fresh.get("refresh_token") or "").strip()
                if rolled_rt and rolled_rt != cred.get("refresh_token"):
                    db.update_registered_fields(email, refresh_token=rolled_rt)
                accounts.append(_sub2_account(cred, fresh))
                state.succeeded += 1
            except Exception as exc:
                state.failed += 1
                state.errors.append({"email": email, "error": _safe_error(exc)})
            finally:
                state.completed += 1
        if accounts:
            state.artifact = _build_artifact(accounts)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            state.filename = (
                f"sub2_{stamp}.json" if len(accounts) > 1
                else f"sub2_{_safe_filename_email(accounts[0]['name'])}_{stamp}.json"
            )
        state.message = (
            f"已生成 {len(accounts)} 个账号；"
            f"跳过 {state.skipped} 个无 RT 账号，失败 {state.failed} 个"
        )

    return _run_async(task, worker), len(eligible), task.skipped


def _login_existing_for_rt(email: str, *, proxy: str = "", otp_timeout: int = 180) -> dict:
    source = db.get_account(email)
    if not source:
        raise RuntimeError("号池中没有该账号的原始四段邮箱凭据")
    if not source.get("client_id") or not source.get("refresh_token"):
        raise RuntimeError("原始 Outlook 四段凭据不完整")

    cfg = Config(proxy=proxy.strip() or None)
    mail = OutlookMailProvider(
        email=source["email"],
        password=source.get("password", ""),
        client_id=source["client_id"],
        refresh_token=source["refresh_token"],
    )
    flow = AuthFlow(cfg)
    # OTP 超时由邮件 provider 从环境读取；仅在当前任务线程内临时设置会污染多线程，
    # 因此显式包一层 provider 方法，向底层传入本任务的超时值。
    original_wait = mail.wait_for_otp

    def wait_for_otp(target_email, timeout=240, issued_after=None):
        return original_wait(
            target_email,
            timeout=max(30, min(int(otp_timeout), 600)),
            issued_after=issued_after,
        )

    mail.wait_for_otp = wait_for_otp
    try:
        result = flow.run_protocol_login(
            mail,
            email,
            password=source.get("password", ""),
            allow_registration_fallback=False,
        )
        data = result.to_dict()
        rt = str(data.get("refresh_token") or "").strip()
        if not rt:
            raise RuntimeError("OTP 登录完成，但未获取到 OpenAI refresh_token")
        # 只更新 RT 及其配套 id_token；不允许临时/不同用途的 AT 覆盖网页 AT。
        db.update_registered_fields(
            email,
            refresh_token=rt,
            id_token=data.get("id_token") or None,
        )
        return db.get_registered(email) or {**data, "email": email}
    finally:
        try:
            flow.session.close()
        except Exception:
            pass


def start_rt_login(
    emails: list[str],
    *,
    proxy: str = "",
    otp_timeout: int = 180,
) -> str:
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    task = _new_task("acquire_rt", len(requested))

    def worker(state: AccountTask) -> None:
        successful: list[dict] = []
        for email in requested:
            state.current_email = email
            state.message = f"等待 OTP 登录 {state.completed + 1}/{state.total}"
            try:
                cred = _login_existing_for_rt(
                    email, proxy=proxy, otp_timeout=otp_timeout,
                )
                state.succeeded += 1
                state.results[email] = {"status": "ok", "label": "RT 获取成功"}
                # 文件生成是附加动作；即使刷新 Codex AT 失败，也不能把已成功的
                # OTP 取 RT 误报成失败。
                try:
                    fresh = exporter.refresh_codex_token(cred["refresh_token"])
                    rolled_rt = str(fresh.get("refresh_token") or "").strip()
                    if rolled_rt and rolled_rt != cred.get("refresh_token"):
                        db.update_registered_fields(email, refresh_token=rolled_rt)
                        cred["refresh_token"] = rolled_rt
                    successful.append(_sub2_account(cred, fresh))
                except Exception as export_exc:
                    state.results[email]["download_error"] = _safe_error(export_exc)
            except Exception as exc:
                state.failed += 1
                error = _safe_error(exc)
                state.errors.append({"email": email, "error": error})
                state.results[email] = {"status": "error", "label": error}
            finally:
                state.completed += 1
        if successful:
            state.artifact = _build_artifact(successful)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            state.filename = (
                f"sub2_{stamp}.json" if len(successful) > 1
                else f"sub2_{_safe_filename_email(successful[0]['name'])}_{stamp}.json"
            )
        state.message = f"RT 获取成功 {state.succeeded} 个，失败 {state.failed} 个"

    return _run_async(task, worker)


def _request_account_status(access_token: str, proxy: str = "") -> tuple[int, dict]:
    cffi = exporter._import_cffi()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    response = cffi.get(
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        },
        proxies=proxies,
        impersonate="chrome110",
        timeout=20,
    )
    try:
        body = response.json() if response.status_code == 200 else {}
    except Exception:
        body = {}
    return int(response.status_code), body


def _jwt_account_id(access_token: str) -> str:
    payload = exporter._decode_jwt_payload(access_token)
    auth = exporter._get_auth(payload)
    return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()


def _select_workspace(data: dict, expected_account_id: str) -> Optional[dict]:
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict) or not accounts:
        return None
    if expected_account_id:
        direct = accounts.get(expected_account_id)
        if isinstance(direct, dict):
            return direct
        for key, value in accounts.items():
            if not isinstance(value, dict):
                continue
            account = value.get("account") if isinstance(value.get("account"), dict) else {}
            candidate = str(
                account.get("id") or account.get("account_id") or key or ""
            ).strip()
            if candidate == expected_account_id:
                return value
        return None
    if len(accounts) == 1:
        value = next(iter(accounts.values()))
        return value if isinstance(value, dict) else None
    return None


def check_account_status(
    cred: dict,
    *,
    proxy: str = "",
    requester: Callable[[str, str], tuple[int, dict]] = _request_account_status,
) -> dict:
    email = cred["email"]
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        return {"status": "no_at", "label": "无 AT", "checked_at": time.time()}

    expected_id = _jwt_account_id(access_token)
    status_code, data = requester(access_token, proxy)
    if status_code == 401:
        session_token = str(cred.get("session_token") or "").strip()
        if not session_token:
            return {
                "status": "credential_invalid",
                "label": "凭证失效",
                "checked_at": time.time(),
            }
        flow = None
        try:
            flow = AuthFlow(Config(proxy=proxy.strip() or None))
            refreshed = flow.from_existing_credentials(
                session_token,
                access_token,
                str(cred.get("device_id") or ""),
            )
            new_access_token = str(refreshed.access_token or "").strip()
            if not new_access_token:
                raise RuntimeError("ST 未返回网页 AT")
            db.update_registered_fields(
                email,
                access_token=new_access_token,
                session_token=refreshed.session_token or None,
                cookie_header=refreshed.cookie_header or None,
            )
            access_token = new_access_token
            expected_id = _jwt_account_id(access_token) or expected_id
            status_code, data = requester(access_token, proxy)
        except Exception:
            status_code, data = 401, {}
        finally:
            try:
                if flow is not None:
                    flow.session.close()
            except Exception:
                pass

    checked_at = time.time()
    if status_code == 401:
        return {"status": "credential_invalid", "label": "凭证失效", "checked_at": checked_at}
    if status_code != 200:
        return {"status": "error", "label": f"HTTP {status_code}", "checked_at": checked_at}

    info = _select_workspace(data, expected_id)
    if not info:
        label = "账号 workspace 不匹配" if expected_id else "多 workspace 无法确认"
        return {"status": "error", "label": label, "checked_at": checked_at}
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    entitlement = info.get("entitlement") if isinstance(info.get("entitlement"), dict) else {}
    promo = (
        info.get("eligible_promo_campaigns")
        if isinstance(info.get("eligible_promo_campaigns"), dict) else {}
    )
    if account.get("is_deactivated") is True:
        return {"status": "banned", "label": "已停用", "checked_at": checked_at}
    plan = str(account.get("plan_type") or "free").lower()
    has_subscription = bool(entitlement.get("has_active_subscription"))
    plus_promo = promo.get("plus") if isinstance(promo.get("plus"), dict) else {}
    if plan == "plus" or has_subscription:
        return {"status": "plus_active", "label": "Plus", "checked_at": checked_at}
    if plus_promo.get("id") == "plus-1-month-free":
        return {"status": "plus_eligible", "label": "Plus 试用", "checked_at": checked_at}
    return {"status": "free", "label": "Free", "checked_at": checked_at}


def start_status_refresh(emails: list[str], *, proxy: str = "") -> str:
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    task = _new_task("refresh_status", len(requested))

    def worker(state: AccountTask) -> None:
        for email in requested:
            state.current_email = email
            state.message = f"正在检查 {state.completed + 1}/{state.total}"
            cred = db.get_registered(email)
            if not cred:
                info = {"status": "not_found", "label": "未找到", "checked_at": time.time()}
                state.failed += 1
            else:
                try:
                    info = check_account_status(cred, proxy=proxy)
                    if info["status"] in {"error", "credential_invalid"}:
                        state.failed += 1
                    else:
                        state.succeeded += 1
                    db.update_plus_check(email, info)
                except Exception as exc:
                    info = {
                        "status": "error",
                        "label": _safe_error(exc),
                        "checked_at": time.time(),
                    }
                    db.update_plus_check(email, info)
                    state.failed += 1
            state.results[email] = info
            state.completed += 1
        state.message = f"状态刷新完成：成功 {state.succeeded}，异常 {state.failed}"

    return _run_async(task, worker)
