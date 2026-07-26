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

from auth_flow import AuthFlow, SmsRequiredError
from config import Config
from log_safety import redact_sensitive_text
from mail_outlook import OutlookMailProvider

from . import db, exporter
from .sms_runtime import build_sms_controller

logger = logging.getLogger("webui.account_ops")
TASK_TTL_SECONDS = 3600
PHASE_LABELS = {
    "queued": "等待执行",
    "prepare": "检查账号资料",
    "login": "登录已有账号",
    "email_otp": "邮箱验证码",
    "web_session": "建立网页会话",
    "codex_oauth": "Codex 授权",
    "phone_verification": "手机验证",
    "persist": "保存 RT",
    "download": "生成下载文件",
    "complete": "任务完成",
    "failed": "任务失败",
}


class AccountOperationError(RuntimeError):
    """可安全展示且带稳定错误码、处理建议的账号运维错误。"""

    def __init__(self, code: str, message: str, action: str = ""):
        super().__init__(message)
        self.code = code
        self.action = action


class AccountTaskBusy(RuntimeError):
    def __init__(self, task_id: str):
        super().__init__("所选账号已有 RT 获取任务运行中")
        self.task_id = task_id


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
    phase: str = "queued"
    phase_label: str = "等待执行"
    phase_detail: str = ""
    action_required: str = ""
    events: list[dict] = field(default_factory=list)
    emails: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def set_phase(
        self,
        phase: str,
        detail: str,
        *,
        status: str = "running",
        email: str = "",
        code: str = "",
    ) -> None:
        safe_detail = redact_sensitive_text(detail, max_length=240)
        self.phase = phase
        self.phase_label = PHASE_LABELS.get(phase, phase)
        self.phase_detail = safe_detail
        event = {
            "phase": phase,
            "label": self.phase_label,
            "detail": safe_detail,
            "status": status,
            "email": _clean_email(email),
            "code": code,
            "at": time.time(),
        }
        if self.events and all(
            self.events[-1].get(key) == event.get(key)
            for key in ("phase", "detail", "status", "email", "code")
        ):
            return
        self.events.append(event)
        if len(self.events) > 40:
            del self.events[:-40]

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
            "phase": self.phase,
            "phase_label": self.phase_label,
            "phase_detail": self.phase_detail,
            "action_required": self.action_required,
            "events": list(self.events),
        }


_tasks: dict[str, AccountTask] = {}
_active_rt_by_email: dict[str, str] = {}
_lock = threading.Lock()


def _clean_email(value: str) -> str:
    return str(value or "").strip().lower()


def _error_payload(exc: Exception) -> dict:
    if isinstance(exc, AccountOperationError):
        code = exc.code
        action = exc.action
    elif isinstance(exc, SmsRequiredError):
        code = "SMS_VERIFICATION_FAILED"
        action = "请检查“运行与配置 → 接码配置”中的余额、国家和 API Key 后重试。"
    else:
        code = "ACCOUNT_TASK_FAILED"
        action = ""
    message = redact_sensitive_text(exc, max_length=180) or exc.__class__.__name__
    return {"code": code, "message": message, "action": action}


def _safe_error(exc: Exception) -> str:
    """兼容原调用方的安全错误文本。"""
    return _error_payload(exc)["message"]


def _safe_filename_email(email: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(email or "").replace("@", "_at_"))
    return cleaned[:120] or "account"


def _prune_tasks_locked() -> None:
    now = time.time()
    expired = [
        key for key, item in _tasks.items()
        if item.finished_at and now - item.finished_at > TASK_TTL_SECONDS
    ]
    for key in expired:
        _tasks.pop(key, None)


def _new_task(kind: str, total: int, *, emails: tuple[str, ...] = ()) -> AccountTask:
    task = AccountTask(
        task_id=uuid.uuid4().hex,
        kind=kind,
        total=total,
        emails=emails,
    )
    with _lock:
        _prune_tasks_locked()
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
            error = _error_payload(exc)
            task.message = error["message"]
            task.action_required = error["action"]
            task.set_phase(
                "failed",
                error["message"],
                status="error",
                code=error["code"],
            )
            logger.warning("账号任务 %s 失败: %s", task.task_id, task.message)
        finally:
            task.current_email = ""
            task.finished_at = time.time()
            if task.kind == "acquire_rt":
                with _lock:
                    for email in task.emails:
                        if _active_rt_by_email.get(email) == task.task_id:
                            _active_rt_by_email.pop(email, None)

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


def _login_existing_for_rt(
    email: str,
    *,
    proxy: str = "",
    otp_timeout: int = 180,
    progress: Optional[Callable[..., None]] = None,
) -> dict:
    emit = progress or (lambda *_args, **_kwargs: None)
    emit("prepare", "正在核对号池中的原始邮箱凭据", status="running")
    source = db.get_account(email)
    if not source:
        raise AccountOperationError(
            "SOURCE_EMAIL_NOT_FOUND",
            "号池中没有该账号的原始四段邮箱凭据。",
            "请先把该账号的 Outlook 四段邮箱重新导入号池。",
        )
    if not source.get("client_id") or not source.get("refresh_token"):
        raise AccountOperationError(
            "SOURCE_EMAIL_INCOMPLETE",
            "原始 Outlook 四段凭据不完整。",
            "请补全 email、password、client_id、refresh_token 后重试。",
        )
    emit("prepare", "原始邮箱凭据检查通过", status="success")

    cfg = Config(proxy=proxy.strip() or None)
    mail = OutlookMailProvider(
        email=source["email"],
        password=source.get("password", ""),
        client_id=source["client_id"],
        refresh_token=source["refresh_token"],
    )
    sms_cfg = db.get_sms_internal_config()

    def sms_log(message: str) -> None:
        emit("phone_verification", message, status="running")

    sms_callback = build_sms_controller(
        sms_cfg,
        log_fn=sms_log,
        require_complete=True,
    )
    flow = AuthFlow(
        cfg,
        sms_callback=sms_callback,
        # RT 是用户主动操作；启用接码后若绑号失败，应明确失败而非静默回退。
        sms_required=bool(sms_cfg.get("sms_enabled")),
    )
    # OTP 超时由邮件 provider 从环境读取；仅在当前任务线程内临时设置会污染多线程，
    # 因此显式包一层 provider 方法，向底层传入本任务的超时值。
    original_wait = mail.wait_for_otp

    def wait_for_otp(target_email, timeout=240, issued_after=None):
        emit("email_otp", "已发出验证码，正在从 Outlook 邮箱读取", status="running")
        code = original_wait(
            target_email,
            timeout=max(30, min(int(otp_timeout), 600)),
            issued_after=issued_after,
        )
        emit("email_otp", "邮箱验证码读取成功", status="success")
        return code

    mail.wait_for_otp = wait_for_otp
    try:
        emit("login", "正在以已有账号模式登录；禁止回落注册", status="running")
        try:
            result = flow.run_protocol_login(
                mail,
                email,
                password=source.get("password", ""),
                allow_registration_fallback=False,
            )
        except SmsRequiredError as exc:
            emit(
                "phone_verification",
                "Codex 要求手机验证，但接码流程未完成",
                status="error",
                code="SMS_VERIFICATION_FAILED",
            )
            raise AccountOperationError(
                "SMS_VERIFICATION_FAILED",
                f"手机验证未完成：{redact_sensitive_text(exc, max_length=120)}",
                "请检查“运行与配置 → 接码配置”中的余额、国家和 API Key 后重试。",
            ) from exc
        data = result.to_dict()
        if data.get("access_token") or data.get("session_token"):
            emit("web_session", "邮箱验证完成，网页会话已建立", status="success")
        rt = str(data.get("refresh_token") or "").strip()
        if not rt:
            code = str(getattr(flow, "codex_rt_error_code", "") or "")
            if code == "PHONE_BINDING_REQUIRED":
                enabled = bool(sms_cfg.get("sms_enabled"))
                action = (
                    "接码已启用但未完成绑号，请检查余额、国家和号码库存后重试。"
                    if enabled else
                    "请前往“运行与配置 → 接码配置”启用接码后重试；启用会产生接码费用。"
                )
                message = "邮箱 OTP 和网页登录均成功，但 Codex 授权要求绑定手机号，RT 未写入。"
                emit(
                    "codex_oauth",
                    message,
                    status="error",
                    code=code,
                )
                raise AccountOperationError(
                    code,
                    message,
                    action,
                )
            detail = str(
                getattr(flow, "codex_rt_error_message", "")
                or "Codex OAuth 未返回 RT"
            )
            message = f"邮箱 OTP 登录成功，但未获取到 OpenAI RT：{detail}。"
            emit(
                "codex_oauth",
                message,
                status="error",
                code=code or "RT_NOT_ISSUED",
            )
            raise AccountOperationError(
                code or "RT_NOT_ISSUED",
                message,
                "请稍后重试；若持续失败，请查看任务阶段中的 Codex 授权结果。",
            )
        emit("codex_oauth", "Codex OAuth 已返回 RT", status="success")
        # 只更新 RT 及其配套 id_token；不允许临时/不同用途的 AT 覆盖网页 AT。
        db.update_registered_fields(
            email,
            refresh_token=rt,
            id_token=data.get("id_token") or None,
        )
        emit("persist", "RT 已安全写入账号缓存，网页 AT 保持不变", status="success")
        return db.get_registered(email) or {**data, "email": email}
    finally:
        if sms_callback is not None:
            try:
                sms_callback.cleanup()
            except Exception:
                pass
        try:
            flow.session.close()
        except Exception:
            pass


def start_rt_login(
    emails: list[str],
    *,
    proxy: str = "",
    otp_timeout: int = 180,
) -> tuple[str, bool]:
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    if not requested:
        raise ValueError("emails 不能为空")
    with _lock:
        _prune_tasks_locked()
        active_ids = {
            _active_rt_by_email[email]
            for email in requested
            if email in _active_rt_by_email
        }
        if active_ids:
            task_id = next(iter(active_ids))
            if len(active_ids) == 1 and all(
                _active_rt_by_email.get(email) == task_id for email in requested
            ):
                return task_id, True
            raise AccountTaskBusy(task_id)
        task = AccountTask(
            task_id=uuid.uuid4().hex,
            kind="acquire_rt",
            total=len(requested),
            emails=tuple(requested),
        )
        _tasks[task.task_id] = task
        for email in requested:
            _active_rt_by_email[email] = task.task_id

    def worker(state: AccountTask) -> None:
        successful: list[dict] = []
        for email in requested:
            state.current_email = email
            state.message = f"正在处理 {state.completed + 1}/{state.total}"

            def progress(phase, detail, *, status="running", code=""):
                state.set_phase(
                    phase,
                    detail,
                    status=status,
                    email=email,
                    code=code,
                )

            try:
                cred = _login_existing_for_rt(
                    email,
                    proxy=proxy,
                    otp_timeout=otp_timeout,
                    progress=progress,
                )
                state.succeeded += 1
                state.results[email] = {
                    "status": "ok",
                    "code": "RT_ACQUIRED",
                    "label": "RT 获取并保存成功",
                }
                # 文件生成是附加动作；即使刷新 Codex AT 失败，也不能把已成功的
                # OTP 取 RT 误报成失败。
                state.set_phase(
                    "download",
                    "正在刷新临时 Codex AT 并生成 Sub2API JSON",
                    email=email,
                )
                try:
                    fresh = exporter.refresh_codex_token(cred["refresh_token"])
                    rolled_rt = str(fresh.get("refresh_token") or "").strip()
                    if rolled_rt and rolled_rt != cred.get("refresh_token"):
                        db.update_registered_fields(email, refresh_token=rolled_rt)
                        cred["refresh_token"] = rolled_rt
                    successful.append(_sub2_account(cred, fresh))
                    state.set_phase(
                        "download",
                        "Sub2API JSON 已生成，等待下载",
                        status="success",
                        email=email,
                    )
                except Exception as export_exc:
                    export_error = _error_payload(export_exc)
                    state.results[email]["download_error"] = export_error["message"]
                    state.set_phase(
                        "download",
                        f"RT 已保存，但文件生成失败：{export_error['message']}",
                        status="warning",
                        email=email,
                        code="DOWNLOAD_BUILD_FAILED",
                    )
            except Exception as exc:
                state.failed += 1
                error = _error_payload(exc)
                state.errors.append({
                    "email": email,
                    "error": error["message"],
                    "code": error["code"],
                    "action": error["action"],
                })
                state.results[email] = {
                    "status": "error",
                    "code": error["code"],
                    "label": error["message"],
                    "action": error["action"],
                }
                if error["action"] and not state.action_required:
                    state.action_required = error["action"]
                state.set_phase(
                    "failed",
                    error["message"],
                    status="error",
                    email=email,
                    code=error["code"],
                )
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
        state.set_phase(
            "complete" if state.failed == 0 else "failed",
            state.message,
            status="success" if state.failed == 0 else "error",
            code="" if state.failed == 0 else "RT_TASK_PARTIAL_OR_FAILED",
        )

    return _run_async(task, worker), False


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
