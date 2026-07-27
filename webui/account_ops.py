"""账号管理后台任务：已有账号取 RT、状态刷新与 Sub2API 文件生成。

任务状态和下载内容只保存在当前进程内存中；下载完成即移除文件内容，
不会把含 token 的导出文件长期写入服务器磁盘。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import random
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from auth_flow import AuthFlow, SmsRequiredError
from config import Config
from log_safety import redact_sensitive_text
from mail_outlook import OutlookMailProvider

from . import db, exporter, link_gen, twofa
from .sms_runtime import build_sms_controller

logger = logging.getLogger("webui.account_ops")
TASK_TTL_SECONDS = 3600
KAKAO_MAX_FULL_ATTEMPTS = 3
KAKAO_ACCOUNT_WORKERS = 5
KAKAO_PROXY_SID_PLACEHOLDER = "{sid}"
PHASE_LABELS = {
    "queued": "等待执行",
    "prepare": "检查账号资料",
    "login": "登录已有账号",
    "email_otp": "邮箱验证码",
    "web_session": "建立网页会话",
    "codex_oauth": "Codex 授权",
    "phone_verification": "手机验证",
    "reauth": "2FA 重认证",
    "enroll": "注册 TOTP",
    "activate": "激活 2FA",
    "persist": "保存 RT",
    "download": "生成下载文件",
    "checkout": "创建 checkout",
    "stripe_init": "初始化 Stripe",
    "update": "应用促销价",
    "attempt": "开始新 checkout",
    "retry": "更换线路重试",
    "confirm": "确认付款方式",
    "approve": "审批并提取链接",
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
_active_2fa_by_email: dict[str, str] = {}
# 同一账号同类任务只允许一个在跑（重复登录/重复 enroll 会互相打断登录态）
_EXCLUSIVE_TASKS: dict[str, dict[str, str]] = {
    "acquire_rt": _active_rt_by_email,
    "bind_2fa": _active_2fa_by_email,
}
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


def _is_retryable_kakao_error(exc: Exception) -> bool:
    """判断 Kakao 提链是否应换线路重建 checkout。"""
    detail = str(exc or "").lower()
    fatal_markers = (
        "checkout_not_kakao_trial",
        "促销未生效",
        "没有可用的网页 access token",
        "http 400",
        "http 401",
        "http 403",
    )
    if any(marker in detail for marker in fatal_markers):
        return False
    retryable_markers = (
        "timeout",
        "timed out",
        "curl: (",
        "代理",
        "proxy",
        "connection",
        "network",
        "http 408",
        "http 429",
        "http 5",
    )
    return any(marker in detail for marker in retryable_markers)


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


def _claim_exclusive_task(kind: str, requested: list[str]) -> tuple[Optional[AccountTask], str]:
    """为一批账号占用某类独占任务。

    返回 (新建任务, 可复用的旧任务 id)；同一批账号命中同一个在跑的任务时复用它，
    命中多个不同任务则抛 AccountTaskBusy 让调用方提示等待。
    """
    registry = _EXCLUSIVE_TASKS[kind]
    with _lock:
        _prune_tasks_locked()
        active_ids = {
            registry[email] for email in requested if email in registry
        }
        if active_ids:
            task_id = next(iter(active_ids))
            if len(active_ids) == 1 and all(
                registry.get(email) == task_id for email in requested
            ):
                return None, task_id
            raise AccountTaskBusy(task_id)
        task = AccountTask(
            task_id=uuid.uuid4().hex,
            kind=kind,
            total=len(requested),
            emails=tuple(requested),
        )
        _tasks[task.task_id] = task
        for email in requested:
            registry[email] = task.task_id
        return task, ""


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
            registry = _EXCLUSIVE_TASKS.get(task.kind)
            if registry is not None:
                with _lock:
                    for email in task.emails:
                        if registry.get(email) == task.task_id:
                            registry.pop(email, None)

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


def _pool_lines(pool_text: str) -> list[str]:
    """把多行代理池拆成干净行列表（忽略空行与 # 注释）。"""
    return [
        ln.strip()
        for ln in str(pool_text or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _pick_proxy(pool_text: str) -> str:
    """从多行代理池里随机取一行（忽略空行与 # 注释）。"""
    lines = _pool_lines(pool_text)
    return random.choice(lines) if lines else ""


def _new_kakao_proxy_sid() -> str:
    """生成 8 位 sticky session ID，用于让每个新 checkout 更换出口 IP。"""
    return secrets.token_hex(4)


def _materialize_proxy_template(proxy: str, sid: str) -> str:
    """将代理中的 ``{sid}`` 占位符物化；普通代理保持不变。"""
    text = str(proxy or "").strip()
    return text.replace(KAKAO_PROXY_SID_PLACEHOLDER, sid) if sid else text


def _kakao_checkout_templates(pool1_lines: list[str], attempt_count: int) -> list[str]:
    """为 Kakao 完整重建选择模板。

    固定代理最多只试池中不同线路；若任一 Kakao 池含 ``{sid}`` sticky 模板，
    则可以在同一条模板上生成 3 个不同会话，每次都对应全新 checkout。
    """
    templates = [line for line in pool1_lines if line]
    if not templates:
        return []
    shuffled = random.sample(templates, k=len(templates))
    return [shuffled[index % len(shuffled)] for index in range(max(1, int(attempt_count)))]


def start_link_gen(emails: list[str], method: str, *, poll_seconds: int = 35) -> str:
    """后台为账号提炼 UPI / Kakao 付款链接，成功后写入 extra_json.links。"""
    method = (method or "").strip().lower()
    if method not in link_gen.METHODS:
        raise ValueError(f"不支持的支付方式: {method}")
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    if not requested:
        raise ValueError("emails 不能为空")

    label = link_gen.METHODS[method]["label"]
    pools = db.get_proxy_pools()
    pool1 = pools.get(f"{method}_pool1", "")
    pool2 = pools.get(f"{method}_pool2", "")
    pool1_lines = _pool_lines(pool1)
    pool2_lines = _pool_lines(pool2)
    if not pool1_lines:
        raise AccountOperationError(
            "PROXY_POOL_EMPTY",
            f"{label} 代理池1 为空，无法提炼链接。",
            f"请先在「{label}」配置标签页填写代理池1。",
        )

    task = _new_task("gen_link", len(requested))

    def worker(state: AccountTask) -> None:
        # get_task() 使用同一把锁生成公开快照，避免并行账号更新
        # results/events 时前端刚好复制到变动中的 dict/list。
        state_lock = _lock

        def process_email(email: str) -> None:
            with state_lock:
                state.current_email = email
                state.message = f"正在提炼 {state.completed + 1}/{state.total}"

            def progress(phase, detail, *, status="running", code=""):
                with state_lock:
                    state.set_phase(phase, detail, status=status, email=email, code=code)

            try:
                cred = db.get_registered(email)
                if not cred:
                    raise AccountOperationError(
                        "ACCOUNT_NOT_FOUND",
                        "账号不在已注册列表里。",
                        "请先完成注册或导入该账号的凭证后重试。",
                    )
                if method == "kakao":
                    has_sid_template = any(
                        KAKAO_PROXY_SID_PLACEHOLDER in line
                        for line in (*pool1_lines, *pool2_lines)
                    )
                    attempt_count = (
                        KAKAO_MAX_FULL_ATTEMPTS
                        if has_sid_template
                        else min(KAKAO_MAX_FULL_ATTEMPTS, len(pool1_lines))
                    )
                    checkout_candidates = _kakao_checkout_templates(pool1_lines, attempt_count)
                else:
                    attempt_count = 1
                    checkout_candidates = [random.choice(pool1_lines)]

                result: dict = {}
                link = ""
                for attempt_index, checkout_template in enumerate(checkout_candidates, start=1):
                    update_template = random.choice(pool2_lines) if pool2_lines else checkout_template
                    # 账单 KR 与 JP/VN 促销必须使用独立 sticky SID。部分代理商会按
                    # SID 而不是「国家 + SID」绑定出口；若共用 SID，先建立的 KR
                    # 会话会把后续 JP/VN 请求也粘到 KR，导致促销预检直接失败。
                    checkout_sid = (
                        _new_kakao_proxy_sid()
                        if method == "kakao" and KAKAO_PROXY_SID_PLACEHOLDER in checkout_template
                        else ""
                    )
                    promotion_sid = (
                        _new_kakao_proxy_sid()
                        if method == "kakao" and KAKAO_PROXY_SID_PLACEHOLDER in update_template
                        else ""
                    )
                    checkout_proxy = _materialize_proxy_template(checkout_template, checkout_sid)
                    update_proxy = _materialize_proxy_template(update_template, promotion_sid)
                    if method == "kakao":
                        progress(
                            "attempt",
                            f"Kakao 第 {attempt_index}/{attempt_count} 次：新建 checkout（KR 与 JP/VN 使用独立 sticky 会话）",
                            status="running",
                            code="KAKAO_NEW_CHECKOUT",
                        )
                    try:
                        result = link_gen.generate_link(
                            str(cred.get("access_token") or ""),
                            method,
                            checkout_proxy=checkout_proxy,
                            update_proxy=update_proxy,
                            checkout_pool=[checkout_proxy] if method == "kakao" else pool1_lines,
                            poll_seconds=poll_seconds,
                            approve_workers=1 if method == "kakao" else 10,
                            log=progress,
                        )
                    except Exception as exc:
                        if (
                            method == "kakao"
                            and attempt_index < attempt_count
                            and _is_retryable_kakao_error(exc)
                        ):
                            progress(
                                "retry",
                                f"本次线路异常（{type(exc).__name__}），更换 KR 线路重新开始",
                                status="warning",
                                code="KAKAO_RETRY_NETWORK",
                            )
                            continue
                        raise
                    link = str(result.get("link") or "").strip()
                    if link or method != "kakao" or attempt_index >= attempt_count:
                        break
                    if method == "kakao" and result.get("retryable") is False:
                        break
                    approve_states = list(result.get("approve_states") or [])
                    retry_reason = str(result.get("retry_reason") or "redirect_not_ready")
                    progress(
                        "retry",
                        f"本次未拿到链接（{retry_reason}，审批={approve_states or ['none']}），更换 KR 线路重新开始",
                        status="warning",
                        code="KAKAO_RETRY_NEW_CHECKOUT",
                    )
                if not link:
                    states = result.get("approve_states") or []
                    retry_reason = str(result.get("retry_reason") or "")
                    if method == "kakao" and result.get("retryable") is False:
                        hint = (
                            f"当前 checkout 返回不可重试状态（{retry_reason or 'unknown'}），"
                            "请刷新账号状态/网页 AT 后再试。"
                        )
                    elif states and all(s == "blocked" for s in states):
                        hint = "OpenAI 风控 block 了所有 approve 出口，换一批住宅代理节点或稍后重试。"
                    elif states and all(s in ("timeout", "error") for s in states):
                        hint = "approve 出口全部超时/异常，代理节点不通，换一批节点重试。"
                    else:
                        hint = "可换一批代理节点或稍后重试。"
                    raise AccountOperationError(
                        "LINK_NOT_FOUND",
                        f"未能提炼到 {label} 链接（金额 {result.get('amount')}）。",
                        hint,
                    )
                db.update_registered_link(email, method, link)
                with state_lock:
                    state.succeeded += 1
                    state.results[email] = {
                        "status": "ok",
                        "code": "LINK_OK",
                        "label": f"{label} 链接已生成",
                        "link": link,
                    }
                progress("complete", f"{label} 链接已生成", status="success")
            except Exception as exc:
                error = _error_payload(exc)
                with state_lock:
                    state.failed += 1
                    state.errors.append({"email": email, **error})
                    state.results[email] = {"status": "error", **error}
                    if error["action"] and not state.action_required:
                        state.action_required = error["action"]
                    state.set_phase(
                        "failed", error["message"], status="error",
                        email=email, code=error["code"],
                    )
            finally:
                with state_lock:
                    state.completed += 1

        if method == "kakao" and len(requested) > 1:
            max_workers = min(KAKAO_ACCOUNT_WORKERS, len(requested))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="kakao-link",
            ) as executor:
                futures = [executor.submit(process_email, email) for email in requested]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        else:
            for email in requested:
                process_email(email)

        with state_lock:
            state.message = (
                f"{label} 链接生成成功 {state.succeeded} 个，失败 {state.failed} 个"
            )

    return _run_async(task, worker)


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
            timeout=max(10, min(int(otp_timeout), 600)),
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
    task, reused_id = _claim_exclusive_task("acquire_rt", requested)
    if task is None:
        return reused_id, True

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


def _reauth_web_session_for_2fa(
    flow,
    email: str,
    *,
    otp_timeout: int,
    emit: Callable[..., None],
) -> dict:
    """走邮箱验证码重认证，换一份 pwd_auth_time 新鲜的网页登录态。"""
    source = db.get_account(email)
    if not source or not source.get("client_id") or not source.get("refresh_token"):
        raise AccountOperationError(
            "SOURCE_EMAIL_NOT_FOUND",
            "绑定 2FA 需要邮箱重认证，但号池里没有该账号可用的原始邮箱凭据。",
            "请先把该账号的 Outlook 四段邮箱重新导入号池后重试。",
        )
    mail = OutlookMailProvider(
        email=source["email"],
        password=source.get("password", ""),
        client_id=source["client_id"],
        refresh_token=source["refresh_token"],
    )
    issued_after = time.time()
    auth_url = twofa.trigger_reauth(flow, email)
    time.sleep(1)
    twofa.follow_reauth(flow, auth_url)
    emit("email_otp", "重认证验证码已发出，正在从 Outlook 邮箱读取", status="running")
    code = mail.wait_for_otp(
        email,
        timeout=max(10, min(int(otp_timeout), 600)),
        issued_after=issued_after,
    )
    emit("email_otp", "邮箱验证码读取成功", status="success")
    continue_url = twofa.validate_reauth_otp(flow, code)
    session = twofa.exchange_web_session(flow, continue_url)
    emit("reauth", "重认证完成，已换到新的网页登录态", status="success")
    return session


def _bind_two_factor(
    email: str,
    *,
    proxy: str = "",
    otp_timeout: int = 180,
    progress: Optional[Callable[..., None]] = None,
) -> str:
    """给已注册账号绑定 TOTP，返回并落库 2FA 密钥。"""
    emit = progress or (lambda *_args, **_kwargs: None)
    cred = db.get_registered(email)
    if not cred:
        raise AccountOperationError(
            "ACCOUNT_NOT_FOUND",
            "账号不在已注册列表里。",
            "请先完成注册或导入该账号的凭证后重试。",
        )
    existing = twofa.normalize_secret(cred.get("totp_secret"))
    if existing:
        emit("complete", "该账号已有 2FA 密钥，无需重复绑定", status="success")
        return existing

    session_token = str(cred.get("session_token") or "").strip()
    access_token = str(cred.get("access_token") or "").strip()
    if not session_token and not access_token:
        raise AccountOperationError(
            "CREDENTIAL_MISSING",
            "该账号没有可用的网页登录态（ST / AT 均为空）。",
            "请先用“取 RT”重新登录该账号，拿到网页登录态后再绑定 2FA。",
        )

    emit("prepare", "正在用已有网页登录态检查账号", status="running")
    flow = AuthFlow(Config(proxy=proxy.strip() or None))
    try:
        flow.from_existing_credentials(
            session_token, access_token, str(cred.get("device_id") or "")
        )
        session = twofa.fetch_web_session(flow)
        if not session["access_token"]:
            raise AccountOperationError(
                "WEB_SESSION_INVALID",
                "网页登录态已失效，无法绑定 2FA。",
                "请先用“取 RT”重新登录该账号刷新登录态后重试。",
            )
        if session["mfa"]:
            raise AccountOperationError(
                "TWO_FA_ALREADY_BOUND",
                "该账号在 OpenAI 侧已经开启二步验证，但本地没有对应密钥。",
                "请在 ChatGPT 网页端关闭二步验证后重试，或手动补录密钥。",
            )
        access_token = session["access_token"]
        emit("prepare", "网页登录态可用，开始绑定 2FA", status="success")

        try:
            secret, session_id = twofa.enroll_totp(flow, access_token)
            emit("enroll", "已直接取到 TOTP 密钥，无需重认证", status="success")
        except twofa.TwoFactorProtocolError as exc:
            # enroll 要求 AT 里的 pwd_auth_time 足够新鲜，过期就得先走邮箱重认证。
            emit(
                "reauth",
                f"登录态需要重新认证（{redact_sensitive_text(exc, max_length=80)}），正在发送邮箱验证码",
                status="running",
            )
            session = _reauth_web_session_for_2fa(
                flow, email, otp_timeout=otp_timeout, emit=emit
            )
            access_token = session["access_token"]
            secret, session_id = twofa.enroll_totp(flow, access_token)
            emit("enroll", "重认证后已取到 TOTP 密钥", status="success")

        twofa.activate_totp(flow, access_token, secret, session_id)
        emit("activate", "2FA 已激活", status="success")

        db.update_registered_fields(
            email,
            totp_secret=twofa.normalize_secret(secret),
            access_token=access_token,
            session_token=session.get("session_token") or None,
            cookie_header=flow._build_chatgpt_cookie_header() or None,
        )
        emit("persist", "2FA 密钥已写入账号缓存", status="success")
        return twofa.normalize_secret(secret)
    except twofa.TwoFactorProtocolError as exc:
        message = f"2FA 绑定失败：{redact_sensitive_text(exc, max_length=140)}"
        emit("failed", message, status="error", code="TWO_FA_BIND_FAILED")
        raise AccountOperationError(
            "TWO_FA_BIND_FAILED",
            message,
            "请稍后重试；若持续失败，请先刷新账号状态确认登录态仍然有效。",
        ) from exc
    finally:
        try:
            flow.session.close()
        except Exception:
            pass


def start_totp_bind(
    emails: list[str],
    *,
    proxy: str = "",
    otp_timeout: int = 180,
) -> tuple[str, bool]:
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    if not requested:
        raise ValueError("emails 不能为空")
    task, reused_id = _claim_exclusive_task("bind_2fa", requested)
    if task is None:
        return reused_id, True

    def worker(state: AccountTask) -> None:
        for email in requested:
            state.current_email = email
            state.message = f"正在绑定 {state.completed + 1}/{state.total}"

            def progress(phase, detail, *, status="running", code=""):
                state.set_phase(phase, detail, status=status, email=email, code=code)

            try:
                _bind_two_factor(
                    email,
                    proxy=proxy,
                    otp_timeout=otp_timeout,
                    progress=progress,
                )
                state.succeeded += 1
                state.results[email] = {
                    "status": "ok",
                    "code": "TWO_FA_BOUND",
                    "label": "2FA 绑定成功",
                }
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
        state.message = f"2FA 绑定成功 {state.succeeded} 个，失败 {state.failed} 个"
        state.set_phase(
            "complete" if state.failed == 0 else "failed",
            state.message,
            status="success" if state.failed == 0 else "error",
            code="" if state.failed == 0 else "TWO_FA_TASK_PARTIAL_OR_FAILED",
        )

    return _run_async(task, worker), False


def two_factor_lines(emails: list[str]) -> tuple[list[str], list[str]]:
    """组装「账号----密码----2FA 密钥」交付行；未绑定 2FA 的账号单独返回。"""
    requested = list(dict.fromkeys(_clean_email(e) for e in emails if _clean_email(e)))
    lines: list[str] = []
    missing: list[str] = []
    for email in requested:
        cred = db.get_registered(email) or {}
        secret = twofa.normalize_secret(cred.get("totp_secret"))
        if not secret:
            missing.append(email)
            continue
        password = str(cred.get("password") or "").strip()
        if not password:
            password = str((db.get_account(email) or {}).get("password") or "").strip()
        lines.append(twofa.copy_line(email, password, secret))
    return lines, missing


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
    # 优惠套餐：OpenAI 对促销订阅返回 plan_type=chatgptplusplan_promo（含 "promo"）。
    # 它本质是折扣版 Plus，必须在 plus_active 之前判定，否则会被 has_subscription
    # 吞成普通 "Plus"，或在无订阅标记时误判成 Free。
    if "promo" in plan:
        return {"status": "plus_promo", "label": "优惠", "checked_at": checked_at}
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
