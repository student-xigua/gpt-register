"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from log_safety import setup_app_logging  # noqa: E402
from . import account_ops, db, proxy_config, registrar  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

setup_app_logging(level=logging.INFO)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SSE_HEARTBEAT_SECONDS = 10
SSE_RETRY_MILLISECONDS = 2000
_SSE_HEARTBEAT = object()

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="多行 4 段格式 (email----password----client_id----refresh_token)")


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    proxy_country: str = ""
    otp_timeout: int = 180
    allow_existing_login: bool = True


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    result = db.import_accounts(req.text)
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(
    status: str = "", limit: int = 50, offset: int = 0,
    search: str = "", registered: str = "",
):
    items = db.list_accounts(
        status=status, limit=limit, offset=offset,
        search=search, registered=registered,
    )
    total = db.count_accounts(status=status, search=search, registered=registered)
    return {"ok": True, "items": items, "total": total}


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.post("/api/accounts/{email}/fetch_code")
def api_fetch_code(email: str):
    """人工接码：从号池原始 Outlook 邮箱现取一次最近的验证码（不占用号池状态）。"""
    row = db.get_account(email)
    if not row:
        raise HTTPException(404, f"号池中没有 {email}")
    if not row.get("refresh_token") and not row.get("password"):
        raise HTTPException(400, "该邮箱缺少可用凭据（refresh_token / 密码均为空）")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from mail_outlook import OutlookMailProvider

    provider = OutlookMailProvider(
        row["email"], row.get("password") or "", row.get("client_id") or "",
        row.get("refresh_token") or "",
    )
    try:
        code = provider.wait_for_otp(
            row["email"], timeout=90, issued_after=time.time() - 1800,
        )
        return {"ok": True, "email": row["email"], "code": code}
    except Exception as e:
        raise HTTPException(504, f"未取到验证码: {e}")


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    is_cf = (mail_source == "cf_temp")
    try:
        task_proxy = req.proxy or (
            proxy_config.pick_working_proxy(
                db.get_global_proxy_template(req.proxy_country),
                api_url=db.get_api_proxy_url(),
                country=req.proxy_country,
            )
            if req.proxy_country else ""
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if is_cf:
        # CF 模式：不需要 outlook 号池，用虚拟占位 account
        import time as _t
        account = {
            "email": f"cf_placeholder_{int(_t.time())}@cf.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
    else:
        account = db.claim_next()
        if not account:
            raise HTTPException(400, "号池里没有 available 账号；请先批量导入")

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": task_proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 推送完整历史 + 实时日志，支持 Last-Event-ID 断线续传。"""
    try:
        after_event_id = max(0, int(request.headers.get("last-event-id") or "0"))
    except (TypeError, ValueError):
        after_event_id = 0

    subscription = registrar.subscribe_run(run_id, after_event_id)
    if subscription is None:
        persisted = registrar.get_persisted_run_events(run_id, after_event_id)
        if persisted is None:
            raise HTTPException(404, "run_id not found")
        history, _ = persisted
        subscriber = None
        finished = True
    else:
        history, subscriber, finished = subscription

    def encode_event(event_id: int, msg: str) -> str:
        if msg == "__END__":
            event_name = "end"
            data = "{}"
        elif msg.startswith("__EVENT__:"):
            event_name = "status"
            data = msg[len("__EVENT__:"):]
        else:
            event_name = "log"
            data = json.dumps({"line": msg}, ensure_ascii=False)
        return f"id: {event_id}\nevent: {event_name}\ndata: {data}\n\n"

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            yield _sse_preamble()
            for event_id, msg in history:
                yield encode_event(event_id, msg)
                if msg == "__END__":
                    return
            if finished:
                yield "event: end\ndata: {}\n\n"
                return

            while True:
                if await request.is_disconnected():
                    break
                item = await loop.run_in_executor(None, _safe_get, subscriber)
                if item is _SSE_HEARTBEAT:
                    yield _sse_heartbeat_frame()
                    continue
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                event_id, msg = item
                yield encode_event(event_id, msg)
                if msg == "__END__":
                    break
        finally:
            registrar.unsubscribe_run(run_id, subscriber)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=SSE_HEARTBEAT_SECONDS)
    except queue.Empty:
        return _SSE_HEARTBEAT


def _sse_preamble():
    return f"retry: {SSE_RETRY_MILLISECONDS}\n: connected\n\n"


def _sse_heartbeat_frame():
    return ": keep-alive\n\n"


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "summary": db.registered_summary(),
    }


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return JSONResponse(
        {"ok": True, "data": row},
        headers={"Cache-Control": "no-store, private"},
    )


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 账号管理工作台 ────────────────────────


class AccountEmailsReq(BaseModel):
    emails: list[str] = Field(..., min_length=1, max_length=100, description="账号邮箱列表")
    proxy: str = Field("", description="可选代理")
    otp_timeout: int = Field(180, ge=10, le=600)


@app.get("/api/account-management/email/{email}")
def api_account_source(email: str):
    """返回该账号导入时的原始 Outlook 四段格式。"""
    row = db.get_account(email)
    if not row:
        raise HTTPException(404, "号池中没有该账号的原始邮箱凭据")
    raw = "----".join(
        str(row.get(key) or "")
        for key in ("email", "password", "client_id", "refresh_token")
    )
    return JSONResponse(
        {"ok": True, "email": row["email"], "raw": raw},
        headers={"Cache-Control": "no-store, private"},
    )


@app.post("/api/account-management/tasks/acquire-rt")
def api_start_acquire_rt(req: AccountEmailsReq):
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    try:
        task_id, reused = account_ops.start_rt_login(
            req.emails,
            proxy=req.proxy,
            otp_timeout=req.otp_timeout,
        )
    except account_ops.AccountTaskBusy as exc:
        raise HTTPException(
            409,
            f"部分所选账号正在另一个 RT 任务中，请等待任务 {exc.task_id[:8]} 完成。",
        ) from exc
    return {"ok": True, "task_id": task_id, "reused": reused}


@app.post("/api/account-management/tasks/bind-2fa")
def api_start_bind_2fa(req: AccountEmailsReq):
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    try:
        task_id, reused = account_ops.start_totp_bind(
            req.emails,
            proxy=req.proxy,
            otp_timeout=req.otp_timeout,
        )
    except account_ops.AccountTaskBusy as exc:
        raise HTTPException(
            409,
            f"部分所选账号正在另一个 2FA 任务中，请等待任务 {exc.task_id[:8]} 完成。",
        ) from exc
    return {"ok": True, "task_id": task_id, "reused": reused}


class TwoFactorCopyReq(BaseModel):
    emails: list[str] = Field(..., min_length=1, max_length=500, description="账号邮箱列表")


@app.post("/api/account-management/2fa/lines")
def api_two_factor_lines(req: TwoFactorCopyReq):
    """返回「账号----密码----2FA 密钥」交付行；未绑定的账号在 missing 里列出。"""
    lines, missing = account_ops.two_factor_lines(req.emails)
    if not lines:
        raise HTTPException(400, "所选账号都还没有 2FA 密钥")
    return JSONResponse(
        {"ok": True, "lines": lines, "missing": missing},
        headers={"Cache-Control": "no-store, private"},
    )


class MarkUsedReq(BaseModel):
    emails: list[str] = Field(..., min_length=1, max_length=500, description="账号邮箱列表")


@app.post("/api/account-management/mark_used")
def api_mark_used(req: MarkUsedReq):
    """把 Plus 账号标记为「已使用」：复制 AT/邮箱或导出 Sub2API 后调用。"""
    n = db.mark_registered_used(req.emails)
    return {"ok": True, "marked": n}


@app.post("/api/account-management/tasks/refresh-status")
def api_start_status_refresh(req: AccountEmailsReq):
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    task_id = account_ops.start_status_refresh(req.emails, proxy=req.proxy)
    return {"ok": True, "task_id": task_id}


@app.post("/api/account-management/tasks/sub2-export")
def api_start_sub2_export(req: AccountEmailsReq):
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    task_id, eligible, skipped = account_ops.start_sub2_export(req.emails)
    if eligible == 0:
        raise HTTPException(400, "所选账号均无 OpenAI RT，未创建下载")
    return {
        "ok": True,
        "task_id": task_id,
        "eligible": eligible,
        "skipped": skipped,
    }


class LinkGenReq(BaseModel):
    emails: list[str] = Field(..., min_length=1, max_length=100, description="账号邮箱列表")
    method: str = Field(..., description="upi / kakao")


@app.post("/api/account-management/tasks/gen-link")
def api_start_gen_link(req: LinkGenReq):
    method = (req.method or "").strip().lower()
    if method not in {"upi", "kakao"}:
        raise HTTPException(400, "method 必须是 upi 或 kakao")
    try:
        task_id = account_ops.start_link_gen(req.emails, method)
    except account_ops.AccountOperationError as exc:
        raise HTTPException(400, f"{exc}（{exc.action}）" if exc.action else str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "task_id": task_id}


@app.get("/api/settings/proxy-pools")
def api_get_proxy_pools():
    return {"ok": True, "config": db.get_proxy_pools(), **db.get_proxy_country_config()}


class SaveProxyPoolsReq(BaseModel):
    upi_pool1: Optional[str] = None
    upi_pool2: Optional[str] = None
    kakao_pool1: Optional[str] = None
    kakao_pool2: Optional[str] = None
    upi_pool1_country: Optional[str] = None
    upi_pool2_country: Optional[str] = None
    kakao_pool1_country: Optional[str] = None
    kakao_pool2_country: Optional[str] = None


@app.post("/api/settings/proxy-pools")
def api_save_proxy_pools(req: SaveProxyPoolsReq):
    data = req.model_dump(exclude_none=True)
    try:
        db.save_proxy_pools(data)
        db.save_proxy_countries(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/account-tasks/{task_id}")
def api_account_task(task_id: str):
    task = account_ops.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已过期")
    return {"ok": True, **task}


@app.get("/api/account-tasks/{task_id}/download")
def api_account_task_download(task_id: str):
    artifact = account_ops.pop_artifact(task_id)
    if not artifact:
        raise HTTPException(404, "文件不存在、尚未生成或已经下载")
    body, filename = artifact
    safe_filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    mail_source: Optional[str] = None       # outlook / cf_temp
    cf_api_url: Optional[str] = None
    cf_admin_token: Optional[str] = None
    cf_domain: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    db.save_mail_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试 CF Temp Email 连通性：创建一个测试地址，确认 admin_token + domain 都对。"""
    mail_source = db.get_setting("mail_source", "outlook")
    if mail_source != "cf_temp":
        raise HTTPException(400, f"当前 mail_source={mail_source}，不需要测试")

    api_url = db.get_setting("cf_api_url", "")
    domain = db.get_setting("cf_domain", "")
    token = db.get_cf_admin_token()
    if not api_url:
        raise HTTPException(400, "未配置 cf_api_url")
    if not domain:
        raise HTTPException(400, "未配置 cf_domain")
    if not token:
        raise HTTPException(400, "未配置 cf_admin_token")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from mail_cf import CFTempEmailProvider
    try:
        provider = CFTempEmailProvider(api_url=api_url, admin_token=token, domain=domain)
        test_email = provider.create_mailbox()
        return {"ok": True, "message": f"连接成功，测试邮箱: {test_email}"}
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_proactive: Optional[str] = None            # "1" = 平台接码失败时不回退环境变量
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """兼容旧控制台：同步刷新当前账号的缓存状态。"""
    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {
                "status": "not_found", "label": "未找到", "checked_at": time.time(),
            }
            continue
        try:
            results[email] = account_ops.check_account_status(
                cred, proxy=req.proxy.strip(),
            )
        except Exception as e:
            results[email] = {
                "status": "error",
                "label": account_ops._safe_error(e),
                "checked_at": time.time(),
            }

    for email, info in results.items():
        if info["status"] != "not_found":
            db.update_plus_check(email, info)

    return {"ok": True, "results": results}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    proxy_country: str = ""      # 国家下拉；每个 run 自动物化独立 {sid}
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 180
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = Field(0, ge=0, le=100000)  # 目标成功数（0=不限量）


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    data = req.model_dump()
    if not data.get("proxy") and not data.get("proxy_pool") and req.proxy_country:
        try:
            data["proxy"] = db.get_global_proxy_template(req.proxy_country)
            data["api_proxy_url"] = db.get_api_proxy_url()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    res = AUTO_LOOP.start(data)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop(force: bool = False):
    res = AUTO_LOOP.stop(force=force)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            yield _sse_preamble()
            while True:
                if await request.is_disconnected():
                    break
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is _SSE_HEARTBEAT:
                    yield _sse_heartbeat_frame()
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/pool")
def pool_page():
    return FileResponse(STATIC_DIR / "pool.html")


@app.get("/accounts")
def accounts_page():
    return FileResponse(STATIC_DIR / "accounts.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)
