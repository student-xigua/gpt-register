"""注册与账号运维任务共用的 SMS controller 构造逻辑。"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from auth_flow import SmsRequiredError
from log_safety import redact_sensitive_text
from sms_provider import PhoneCallbackController

from . import db


logger = logging.getLogger("webui.sms_runtime")


def build_sms_controller(
    cfg: Optional[dict] = None,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    require_complete: bool = False,
) -> Optional[PhoneCallbackController]:
    """按 WebUI 配置创建 controller。

    ``require_complete`` 用于用户主动发起的 RT 任务：既然已经启用接码，
    缺 API key 或 controller 初始化失败就应明确失败，不能静默回退。
    """
    cfg = dict(cfg or db.get_sms_internal_config())
    if not cfg.get("sms_enabled"):
        return None

    api_key = str(cfg.get("sms_api_key") or "").strip()
    strict = bool(require_complete or cfg.get("sms_proactive"))
    if not api_key:
        if strict:
            raise SmsRequiredError("接码已启用，但未配置 SMS API Key")
        logger.warning("[sms] 已启用接码但未配置 SMS API Key，跳过")
        return None

    target_log = log_fn or logger.info

    def safe_log(message: str) -> None:
        target_log(redact_sensitive_text(message, max_length=240))

    try:
        return PhoneCallbackController(
            provider_key=str(cfg.get("sms_provider") or "smsbower"),
            config=cfg,
            service=str(cfg.get("sms_service") or "openai"),
            country=str(cfg.get("sms_country") or "52"),
            log_fn=safe_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
        )
    except Exception as exc:
        safe_error = redact_sensitive_text(exc, max_length=180)
        if strict:
            raise SmsRequiredError(f"接码 controller 创建失败：{safe_error}") from exc
        logger.warning("[sms] 创建接码 controller 失败：%s", safe_error)
        return None
