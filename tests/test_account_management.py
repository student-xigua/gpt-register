from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from auth_flow import AuthFlow, AuthResult
from log_safety import redact_sensitive_text
from webui import account_ops, app, db, twofa


def jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{encoded}.signature"


class TempDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()
        with account_ops._lock:
            account_ops._tasks.clear()
            account_ops._active_rt_by_email.clear()
            account_ops._active_2fa_by_email.clear()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        with account_ops._lock:
            account_ops._tasks.clear()
            account_ops._active_rt_by_email.clear()
            account_ops._active_2fa_by_email.clear()
        self.tempdir.cleanup()

    def save(self, email: str, *, rt: str = "", at: str = "web-at", st: str = "web-st"):
        db.save_registered({
            "email": email,
            "access_token": at,
            "session_token": st,
            "refresh_token": rt,
        })

    def wait_task(self, task_id: str) -> dict:
        deadline = time.time() + 2
        while time.time() < deadline:
            task = account_ops.get_task(task_id)
            if task and task["state"] in {"done", "partial", "failed"}:
                return task
            time.sleep(0.01)
        self.fail("background task did not finish")


class ManualOAuthEndpointTests(TempDatabaseTest):
    def test_start_requires_registered_account(self):
        with self.assertRaises(Exception):
            app.api_manual_oauth_start(
                app.ManualOAuthStartReq(email="missing@example.com")
            )

    def test_complete_persists_rt_without_overwriting_web_at(self):
        self.save("person@example.com", at="web-at", rt="")
        content = b'{"version":1,"accounts":[],"proxies":[]}'
        with mock.patch.object(
            app.manual_oauth,
            "complete_flow",
            return_value={
                "email": "person@example.com",
                "refresh_token": "manual-rt",
                "id_token": "manual-id",
                "filename": "sub2_person.json",
                "content": content,
            },
        ):
            response = app.api_manual_oauth_complete(
                app.ManualOAuthCompleteReq(
                    flow_id="flow-identifier",
                    callback_url="http://localhost:1455/auth/callback?code=x&state=y",
                )
            )
        payload = json.loads(response.body)
        saved = db.get_registered("person@example.com")

        self.assertEqual(saved["refresh_token"], "manual-rt")
        self.assertEqual(saved["id_token"], "manual-id")
        self.assertEqual(saved["access_token"], "web-at")
        self.assertEqual(base64.b64decode(payload["content_b64"]), content)
        self.assertIn("no-store", response.headers["cache-control"])


class Sub2DownloadTests(TempDatabaseTest):
    def setUp(self):
        super().setUp()
        self.token = jwt({
            "https://api.openai.com/profile": {"email": "ready@example.com"},
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-ready"},
        })

    def test_single_export_format_filters_no_rt_and_persists_rolling_rt(self):
        self.save("ready@example.com", rt="old-rt", at="web-at")
        self.save("missing@example.com", rt="", at="other-web-at")
        with mock.patch.object(
            account_ops.exporter,
            "refresh_codex_token",
            return_value={"access_token": self.token, "refresh_token": "rolled-rt"},
        ) as refresh:
            task_id, eligible, skipped = account_ops.start_sub2_export([
                "ready@example.com", "missing@example.com",
            ])
            task = self.wait_task(task_id)

        self.assertEqual((eligible, skipped), (1, 1))
        self.assertTrue(task["download_ready"])
        body, filename = account_ops.pop_artifact(task_id)
        payload = json.loads(body)
        self.assertTrue(filename.endswith(".json"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "ready@example.com")
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["credentials"]["access_token"], self.token)
        self.assertEqual(account["credentials"]["refresh_token"], "rolled-rt")
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "acct-ready")
        refresh.assert_called_once_with("old-rt")
        stored = db.get_registered("ready@example.com")
        self.assertEqual(stored["refresh_token"], "rolled-rt")
        self.assertEqual(stored["access_token"], "web-at")

    def test_all_no_rt_does_not_refresh_or_create_file(self):
        self.save("missing@example.com", rt="")
        with mock.patch.object(account_ops.exporter, "refresh_codex_token") as refresh:
            task_id, eligible, skipped = account_ops.start_sub2_export(["missing@example.com"])
            task = self.wait_task(task_id)
        self.assertEqual((eligible, skipped), (0, 1))
        self.assertFalse(task["download_ready"])
        refresh.assert_not_called()

    def test_download_response_is_attachment_no_store_and_one_shot(self):
        task = account_ops._new_task("sub2_export", 1)
        task.state = "done"
        task.artifact = b'{"version":1,"accounts":[],"proxies":[]}'
        task.filename = "sub2_test.json"
        response = app.api_account_task_download(task.task_id)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("no-store", response.headers["cache-control"])
        with self.assertRaises(Exception):
            app.api_account_task_download(task.task_id)


class ExistingAccountRtTests(TempDatabaseTest):
    def test_codex_add_phone_records_safe_failure_code(self):
        flow = object.__new__(AuthFlow)
        flow._codex_rt_attempted = False
        flow._sms_callback = None
        flow._sms_required = False
        flow.codex_rt_error_code = ""
        flow.codex_rt_error_message = ""
        flow._build_codex_authorize = mock.Mock(return_value=(
            "https://auth.openai.com/oauth/authorize?prompt=login",
            "state",
            "verifier",
            "http://localhost/callback",
            "client",
        ))
        flow._follow_authorize_for_callback = mock.Mock(
            return_value=("", "https://auth.openai.com/add-phone")
        )
        flow._exchange_codex_callback_code = mock.Mock()

        self.assertFalse(flow.oauth_codex_rt_exchange())
        self.assertEqual(flow.codex_rt_error_code, "PHONE_BINDING_REQUIRED")
        self.assertIn("绑定手机号", flow.codex_rt_error_message)
        flow._exchange_codex_callback_code.assert_not_called()

    def test_fresh_protocol_login_never_allows_registration_fallback(self):
        db.import_accounts(
            "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        )
        self.save("person@example.com", rt="", at="original-web-at")
        seen = {}

        class FakeSession:
            def close(self):
                pass

        class FakeFlow:
            def __init__(self, _config, sms_callback=None, sms_required=False):
                self.session = FakeSession()
                seen["sms_callback"] = sms_callback
                seen["sms_required"] = sms_required

            def run_protocol_login(
                self, mail, email, password="", *, allow_registration_fallback=True,
            ):
                seen.update({
                    "mail": mail,
                    "email": email,
                    "password": password,
                    "allow": allow_registration_fallback,
                })
                result = AuthResult()
                result.email = email
                result.access_token = "temporary-login-at"
                result.refresh_token = "new-openai-rt"
                result.id_token = "new-id"
                return result

        with (
            mock.patch.object(account_ops, "AuthFlow", FakeFlow),
            mock.patch.object(account_ops, "OutlookMailProvider") as provider,
        ):
            provider.return_value.wait_for_otp = mock.Mock()
            account_ops._login_existing_for_rt("person@example.com")

        self.assertFalse(seen["allow"])
        self.assertIsNone(seen["sms_callback"])
        self.assertFalse(seen["sms_required"])
        self.assertEqual(seen["password"], "mail-pass")
        source_call = provider.call_args.kwargs
        self.assertEqual(source_call["client_id"], "client-id")
        self.assertEqual(source_call["refresh_token"], "mail-refresh-token-long")
        stored = db.get_registered("person@example.com")
        self.assertEqual(stored["refresh_token"], "new-openai-rt")
        self.assertEqual(stored["access_token"], "original-web-at")

    def test_phone_binding_required_has_clear_code_and_action(self):
        db.import_accounts(
            "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        )
        self.save("person@example.com", rt="", at="original-web-at")

        class FakeSession:
            def close(self):
                pass

        class FakeFlow:
            codex_rt_error_code = "PHONE_BINDING_REQUIRED"
            codex_rt_error_message = "Codex 授权要求绑定手机号"

            def __init__(self, _config, **_kwargs):
                self.session = FakeSession()

            def run_protocol_login(self, *_args, **_kwargs):
                result = AuthResult()
                result.access_token = "temporary-login-at"
                result.session_token = "temporary-session"
                return result

        with (
            mock.patch.object(account_ops, "AuthFlow", FakeFlow),
            mock.patch.object(account_ops, "OutlookMailProvider"),
        ):
            task_id, reused = account_ops.start_rt_login(["person@example.com"])
            task = self.wait_task(task_id)

        self.assertFalse(reused)
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["errors"][0]["code"], "PHONE_BINDING_REQUIRED")
        self.assertIn("邮箱 OTP 和网页登录均成功", task["errors"][0]["error"])
        self.assertIn("启用接码", task["action_required"])
        self.assertEqual(db.get_registered("person@example.com")["refresh_token"], "")
        self.assertFalse(task["download_ready"])

    def test_enabled_sms_controller_is_injected_into_fresh_flow(self):
        db.import_accounts(
            "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        )
        self.save("person@example.com", rt="")
        controller = mock.Mock()
        seen = {}

        class FakeSession:
            def close(self):
                pass

        class FakeFlow:
            def __init__(self, _config, sms_callback=None, sms_required=False):
                self.session = FakeSession()
                seen["callback"] = sms_callback
                seen["required"] = sms_required

            def run_protocol_login(self, *_args, **_kwargs):
                result = AuthResult()
                result.refresh_token = "new-openai-rt"
                return result

        sms_cfg = {
            "sms_enabled": True,
            "sms_provider": "herosms",
            "sms_api_key": "configured",
        }
        with (
            mock.patch.object(account_ops.db, "get_sms_internal_config", return_value=sms_cfg),
            mock.patch.object(account_ops, "build_sms_controller", return_value=controller) as build,
            mock.patch.object(account_ops, "AuthFlow", FakeFlow),
            mock.patch.object(account_ops, "OutlookMailProvider"),
        ):
            account_ops._login_existing_for_rt("person@example.com")

        build.assert_called_once_with(
            sms_cfg,
            log_fn=mock.ANY,
            require_complete=True,
        )
        self.assertIs(seen["callback"], controller)
        self.assertTrue(seen["required"])

    def test_duplicate_rt_click_reuses_running_task(self):
        started = threading.Event()
        release = threading.Event()

        def slow_login(email, **_kwargs):
            started.set()
            release.wait(1)
            return {"email": email, "refresh_token": "rt"}

        with (
            mock.patch.object(account_ops, "_login_existing_for_rt", side_effect=slow_login),
            mock.patch.object(
                account_ops.exporter,
                "refresh_codex_token",
                side_effect=RuntimeError("download unavailable"),
            ),
        ):
            first_id, first_reused = account_ops.start_rt_login(["person@example.com"])
            self.assertTrue(started.wait(1))
            second_id, second_reused = account_ops.start_rt_login(["person@example.com"])
            release.set()
            self.wait_task(first_id)

        self.assertFalse(first_reused)
        self.assertTrue(second_reused)
        self.assertEqual(second_id, first_id)


class TwoFactorTests(TempDatabaseTest):
    SECRET = "JBSWY3DPEHPK3PXP"

    def setUp(self):
        super().setUp()
        self.flow = mock.Mock()
        self.flow._build_chatgpt_cookie_header.return_value = "safe-cookie"
        self.web_session = {
            "access_token": "fresh-web-at",
            "session_token": "fresh-st",
            "mfa": False,
            "email": "person@example.com",
        }

    def patch_flow(self):
        return mock.patch.object(account_ops, "AuthFlow", return_value=self.flow)

    def test_totp_matches_rfc6238_reference_vector(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(twofa.totp_now(secret, at=59), "287082")
        self.assertEqual(twofa.totp_now(secret, at=1111111109), "081804")
        self.assertEqual(twofa.totp_now("jbswy 3dpe-hpk3pxp"), twofa.totp_now(self.SECRET))

    def test_direct_enroll_persists_secret_without_email_reauth(self):
        self.save("person@example.com", at="stale-web-at")
        with (
            self.patch_flow(),
            mock.patch.object(account_ops.twofa, "fetch_web_session", return_value=self.web_session),
            mock.patch.object(
                account_ops.twofa, "enroll_totp", return_value=(self.SECRET, "sess-1")
            ),
            mock.patch.object(account_ops.twofa, "activate_totp") as activate,
            mock.patch.object(account_ops.twofa, "trigger_reauth") as reauth,
        ):
            task_id, reused = account_ops.start_totp_bind(["person@example.com"])
            task = self.wait_task(task_id)

        self.assertFalse(reused)
        self.assertEqual(task["state"], "done")
        self.assertEqual(task["succeeded"], 1)
        reauth.assert_not_called()
        activate.assert_called_once_with(self.flow, "fresh-web-at", self.SECRET, "sess-1")
        stored = db.get_registered("person@example.com")
        self.assertEqual(stored["totp_secret"], self.SECRET)
        self.assertEqual(stored["access_token"], "fresh-web-at")
        row = db.list_registered()[0]
        self.assertEqual(row["totp_len"], len(self.SECRET))

    def test_stale_login_falls_back_to_mailbox_reauth(self):
        db.import_accounts(
            "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        )
        self.save("person@example.com")
        reauth_session = {**self.web_session, "access_token": "reauth-web-at"}
        with (
            self.patch_flow(),
            mock.patch.object(account_ops.time, "sleep"),
            mock.patch.object(account_ops.twofa, "fetch_web_session", return_value=self.web_session),
            mock.patch.object(
                account_ops.twofa,
                "enroll_totp",
                side_effect=[
                    twofa.TwoFactorProtocolError("注册 TOTP 失败 (HTTP 401)", status=401),
                    (self.SECRET, "sess-2"),
                ],
            ),
            mock.patch.object(account_ops.twofa, "activate_totp") as activate,
            mock.patch.object(account_ops.twofa, "trigger_reauth", return_value="https://auth/x"),
            mock.patch.object(account_ops.twofa, "follow_reauth"),
            mock.patch.object(
                account_ops.twofa, "validate_reauth_otp", return_value="https://continue"
            ) as validate,
            mock.patch.object(
                account_ops.twofa, "exchange_web_session", return_value=reauth_session
            ),
            mock.patch.object(account_ops, "OutlookMailProvider") as provider,
        ):
            provider.return_value.wait_for_otp.return_value = "123456"
            secret = account_ops._bind_two_factor("person@example.com", otp_timeout=10)

        self.assertEqual(secret, self.SECRET)
        validate.assert_called_once_with(self.flow, "123456")
        activate.assert_called_once_with(self.flow, "reauth-web-at", self.SECRET, "sess-2")
        self.assertEqual(provider.call_args.kwargs["client_id"], "client-id")
        self.assertEqual(
            provider.return_value.wait_for_otp.call_args.kwargs["timeout"], 10
        )
        self.assertEqual(db.get_registered("person@example.com")["totp_secret"], self.SECRET)

    def test_account_with_openai_side_2fa_fails_with_actionable_code(self):
        self.save("person@example.com")
        with (
            self.patch_flow(),
            mock.patch.object(
                account_ops.twofa,
                "fetch_web_session",
                return_value={**self.web_session, "mfa": True},
            ),
            mock.patch.object(account_ops.twofa, "enroll_totp") as enroll,
        ):
            task = self.wait_task(account_ops.start_totp_bind(["person@example.com"])[0])

        enroll.assert_not_called()
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["errors"][0]["code"], "TWO_FA_ALREADY_BOUND")
        self.assertIn("关闭二步验证", task["action_required"])
        self.assertEqual(db.get_registered("person@example.com")["totp_secret"], "")

    def test_bound_account_is_idempotent_and_never_touches_network(self):
        self.save("person@example.com")
        db.update_registered_fields("person@example.com", totp_secret=self.SECRET)
        with mock.patch.object(account_ops, "AuthFlow") as flow_cls:
            task = self.wait_task(account_ops.start_totp_bind(["person@example.com"])[0])
        flow_cls.assert_not_called()
        self.assertEqual(task["state"], "done")

    def test_duplicate_bind_click_reuses_running_task(self):
        started = threading.Event()
        release = threading.Event()

        def slow_bind(_email, **_kwargs):
            started.set()
            release.wait(1)
            return self.SECRET

        with mock.patch.object(account_ops, "_bind_two_factor", side_effect=slow_bind):
            first_id, first_reused = account_ops.start_totp_bind(["person@example.com"])
            self.assertTrue(started.wait(1))
            second_id, second_reused = account_ops.start_totp_bind(["person@example.com"])
            release.set()
            self.wait_task(first_id)

        self.assertFalse(first_reused)
        self.assertTrue(second_reused)
        self.assertEqual(second_id, first_id)

    def test_copy_lines_join_account_password_secret_and_report_missing(self):
        db.import_accounts(
            "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        )
        self.save("person@example.com")
        self.save("plain@example.com")
        db.update_registered_fields("person@example.com", totp_secret=self.SECRET)

        response = app.api_two_factor_lines(
            app.TwoFactorCopyReq(emails=["person@example.com", "plain@example.com"])
        )
        payload = json.loads(response.body)

        self.assertEqual(
            payload["lines"], [f"person@example.com----mail-pass----{self.SECRET}"]
        )
        self.assertEqual(payload["missing"], ["plain@example.com"])
        self.assertIn("no-store", response.headers["cache-control"])

    def test_copy_lines_reject_request_without_any_secret(self):
        self.save("plain@example.com")
        with self.assertRaises(Exception):
            app.api_two_factor_lines(app.TwoFactorCopyReq(emails=["plain@example.com"]))

    def test_resaving_account_keeps_existing_secret(self):
        self.save("person@example.com")
        db.update_registered_fields("person@example.com", totp_secret=self.SECRET)
        self.save("person@example.com", at="re-registered-at")
        self.assertEqual(db.get_registered("person@example.com")["totp_secret"], self.SECRET)


class PlanFilterTests(TempDatabaseTest):
    def seed(self, email: str, status: str, label: str):
        self.save(email, rt="rt" if status == "plus_active" else "")
        db.update_plus_check(email, {"status": status, "label": label, "checked_at": 1.0})

    def test_plus_filter_matches_the_plus_metric_scope(self):
        self.seed("active@example.com", "plus_active", "Plus")
        self.seed("promo@example.com", "plus_promo", "优惠")
        self.seed("trial@example.com", "plus_eligible", "Plus 试用")
        self.seed("free@example.com", "free", "Free")
        self.seed("banned@example.com", "banned", "已停用")
        self.save("unchecked@example.com")

        result = app.api_registered(limit=50, filter="plus")

        self.assertEqual(
            sorted(item["email"] for item in result["items"]),
            ["active@example.com", "promo@example.com", "trial@example.com"],
        )
        self.assertEqual(result["total"], 3)
        # 顶部「Plus / 优惠 / 试用」卡片与筛选必须同口径
        self.assertEqual(result["summary"]["plus"], 3)
        self.assertEqual(db.count_registered("all"), 6)

    def test_plus_filter_paginates_on_the_filtered_set(self):
        for index in range(5):
            self.seed(f"plus{index}@example.com", "plus_active", "Plus")
            self.seed(f"free{index}@example.com", "free", "Free")

        first = app.api_registered(limit=3, offset=0, filter="plus")
        second = app.api_registered(limit=3, offset=3, filter="plus")

        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["items"]), 3)
        self.assertEqual(len(second["items"]), 2)
        self.assertTrue(
            all(item["email"].startswith("plus") for item in first["items"] + second["items"])
        )

    def test_unknown_filter_falls_back_to_all(self):
        self.seed("free@example.com", "free", "Free")
        self.assertEqual(app.api_registered(filter="'; DROP TABLE registered--")["total"], 1)

    def test_account_search_is_case_insensitive_and_combines_with_filter(self):
        self.seed("Alpha.Plus@outlook.com", "plus_active", "Plus")
        self.seed("alpha.free@outlook.com", "free", "Free")
        self.seed("other@outlook.com", "plus_active", "Plus")

        all_matches = app.api_registered(search="ALPHA")
        plus_matches = app.api_registered(filter="plus", search="alpha")

        self.assertEqual(all_matches["total"], 2)
        self.assertEqual(
            {item["email"] for item in all_matches["items"]},
            {"alpha.plus@outlook.com", "alpha.free@outlook.com"},
        )
        self.assertEqual(plus_matches["total"], 1)
        self.assertEqual(plus_matches["items"][0]["email"], "alpha.plus@outlook.com")

    def test_account_search_treats_like_wildcards_as_text(self):
        self.save("normal@example.com")
        self.assertEqual(app.api_registered(search="%_")["total"], 0)

    def test_plan_status_column_is_backfilled_from_legacy_extra_json(self):
        legacy = Path(self.tempdir.name) / "legacy.db"
        original = db.DB_PATH
        db.DB_PATH = legacy
        try:
            con = db._conn()
            con.execute("""
                CREATE TABLE registered (
                    email TEXT PRIMARY KEY, password TEXT, access_token TEXT,
                    session_token TEXT, refresh_token TEXT, id_token TEXT,
                    device_id TEXT, csrf_token TEXT, cookie_header TEXT,
                    extra_json TEXT, created_at REAL
                )
            """)
            con.execute(
                "INSERT INTO registered (email, extra_json, created_at) VALUES (?, ?, ?)",
                ("legacy@example.com", json.dumps({"plus_check": {"status": "plus_promo"}}), 1.0),
            )
            con.commit()
            con.close()

            db.init_db()

            self.assertEqual(db.count_registered("plus"), 1)
            self.assertEqual(db.registered_summary()["plus"], 1)
        finally:
            db.DB_PATH = original


class UsageStatusTests(TempDatabaseTest):
    def seed(self, email: str, status: str):
        self.save(email)
        db.update_plus_check(email, {"status": status, "label": status, "checked_at": 1.0})

    def test_mark_used_only_updates_detected_plus_accounts_in_mixed_batch(self):
        statuses = {
            "active@example.com": "plus_active",
            "promo@example.com": "plus_promo",
            "trial@example.com": "plus_eligible",
            "free@example.com": "free",
        }
        for email, status in statuses.items():
            self.seed(email, status)
        self.save("unchecked@example.com")

        response = app.api_mark_used(
            app.MarkUsedReq(emails=[*statuses, "unchecked@example.com"])
        )

        self.assertEqual(response, {"ok": True, "marked": 3})
        for email in ("active@example.com", "promo@example.com", "trial@example.com"):
            self.assertIsNotNone(db.get_registered(email)["used_at"])
        self.assertIsNone(db.get_registered("free@example.com")["used_at"])
        self.assertIsNone(db.get_registered("unchecked@example.com")["used_at"])

        used = app.api_registered(filter="used")
        self.assertEqual(used["summary"]["used"], 3)
        self.assertEqual(
            {item["email"] for item in used["items"]},
            {"active@example.com", "promo@example.com", "trial@example.com"},
        )

    def test_copy_email_frontend_marks_only_after_clipboard_write_succeeds(self):
        source = (Path(app.__file__).parent / "static" / "accounts.js").read_text(
            encoding="utf-8"
        )
        start = source.index("async function copySourceEmails")
        end = source.index("\nasync function copyTwoFactor", start)
        copy_source_emails = source[start:end]

        self.assertIn("copiedEmails.push(email);", copy_source_emails)
        self.assertIn("await markUsed(copiedEmails);", copy_source_emails)
        self.assertGreater(
            copy_source_emails.index("await markUsed(copiedEmails);"),
            copy_source_emails.index('await copyText(values.join("\\n"));'),
        )


class BulkDeleteRegisteredTests(TempDatabaseTest):
    def test_bulk_delete_selected_registered_accounts_only(self):
        for email in ("bad-one@example.com", "bad-two@example.com", "keep@example.com"):
            self.save(email)

        response = app.api_bulk_delete_registered(
            app.BulkDeleteRegisteredReq(
                emails=["bad-one@example.com", "bad-two@example.com"]
            )
        )

        self.assertEqual(response, {"ok": True, "deleted": 2, "by": "emails"})
        self.assertIsNone(db.get_registered("bad-one@example.com"))
        self.assertIsNone(db.get_registered("bad-two@example.com"))
        self.assertIsNotNone(db.get_registered("keep@example.com"))

    def test_account_page_exposes_confirmed_bulk_delete_action(self):
        static_dir = Path(app.__file__).parent / "static"
        html = (static_dir / "accounts.html").read_text(encoding="utf-8")
        source = (static_dir / "accounts.js").read_text(encoding="utf-8")
        start = source.index("async function deleteSelectedAccounts")
        end = source.index("\nasync function", start + 1)
        delete_selected = source[start:end]

        self.assertIn('id="deleteSelectedBtn"', html)
        self.assertIn("删除选中", html)
        self.assertIn('confirm(`确定删除选中的 ${emails.length} 个账号注册凭证？', delete_selected)
        self.assertIn('api("api/registered/bulk_delete"', delete_selected)
        self.assertIn("body: JSON.stringify({ emails })", delete_selected)
        self.assertGreater(
            delete_selected.index('api("api/registered/bulk_delete"'),
            delete_selected.index("confirm("),
        )

    def test_account_page_exposes_debounced_server_side_search(self):
        static_dir = Path(app.__file__).parent / "static"
        html = (static_dir / "accounts.html").read_text(encoding="utf-8")
        source = (static_dir / "accounts.js").read_text(encoding="utf-8")

        self.assertIn('id="searchInput"', html)
        self.assertIn('type="search"', html)
        self.assertIn("state.search = event.target.value.trim();", source)
        self.assertIn("search=${encodeURIComponent(state.search)}", source)
        self.assertIn("setTimeout(() =>", source)
        self.assertNotIn('id="proxyInput"', html)
        self.assertNotIn('$("#proxyInput")', source)

    def test_account_page_exposes_manual_sub2_oauth_flow(self):
        static_dir = Path(app.__file__).parent / "static"
        html = (static_dir / "accounts.html").read_text(encoding="utf-8")
        source = (static_dir / "accounts.js").read_text(encoding="utf-8")

        self.assertIn('data-action="manual-sub2"', source)
        self.assertNotIn('data-action="fetch-code"', source)
        self.assertIn("api/account-management/manual-oauth/start", source)
        self.assertIn("api/account-management/manual-oauth/complete", source)
        self.assertIn('id="manualOauthModal"', html)
        self.assertIn("localhost:1455/auth/callback", html)
        self.assertIn("accounts.js?v=20260729-4", html)


class LogSafetyTests(unittest.TestCase):
    def test_redacts_credentials_but_keeps_diagnostic_meaning(self):
        text = redact_sensitive_text(
            'OTP=123456 refresh_token="secret-rt" '
            'Authorization: Bearer abc.def.ghi code_verifier=verifier-value'
        )
        self.assertNotIn("123456", text)
        self.assertNotIn("secret-rt", text)
        self.assertNotIn("verifier-value", text)
        self.assertIn("refresh_token", text)
        self.assertIn("[hidden]", text)


class EmailSourceTests(TempDatabaseTest):
    def test_original_four_part_email_is_returned_without_cache(self):
        raw = "person@example.com----mail-pass----client-id----mail-refresh-token-long"
        db.import_accounts(raw)
        response = app.api_account_source("person@example.com")
        payload = json.loads(response.body)
        self.assertEqual(payload["raw"], raw)
        self.assertIn("no-store", response.headers["cache-control"])


class StatusRefreshTests(TempDatabaseTest):
    def _credential(self, *, account_id="acct-target", st="session-token") -> dict:
        token = jwt({
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            "https://api.openai.com/profile": {"email": "person@example.com"},
        })
        return {
            "email": "person@example.com",
            "access_token": token,
            "session_token": st,
            "device_id": "device",
        }

    def test_401_without_st_is_credential_invalid_not_banned(self):
        result = account_ops.check_account_status(
            self._credential(st=""),
            requester=lambda _at, _proxy: (401, {}),
        )
        self.assertEqual(result["status"], "credential_invalid")

    def test_401_refreshes_web_at_with_st_then_retries(self):
        old = self._credential()
        self.save(old["email"], at=old["access_token"], st=old["session_token"])
        new_at = jwt({
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-target"},
        })
        refreshed = AuthResult()
        refreshed.access_token = new_at
        refreshed.session_token = "rolled-session"
        refreshed.cookie_header = "safe-cookie"
        flow = mock.Mock()
        flow.from_existing_credentials.return_value = refreshed
        calls = []

        def requester(token, _proxy):
            calls.append(token)
            if len(calls) == 1:
                return 401, {}
            return 200, {
                "accounts": {
                    "acct-target": {
                        "account": {"id": "acct-target", "plan_type": "free"},
                        "entitlement": {"has_active_subscription": False},
                    }
                }
            }

        with mock.patch.object(account_ops, "AuthFlow", return_value=flow):
            result = account_ops.check_account_status(old, requester=requester)

        self.assertEqual(result["status"], "free")
        self.assertEqual(calls, [old["access_token"], new_at])
        stored = db.get_registered(old["email"])
        self.assertEqual(stored["access_token"], new_at)
        self.assertEqual(stored["session_token"], "rolled-session")

    def test_multi_workspace_matches_token_account_id_not_first(self):
        data = {
            "accounts": {
                "acct-other": {
                    "account": {"id": "acct-other", "plan_type": "plus"},
                    "entitlement": {"has_active_subscription": True},
                },
                "acct-target": {
                    "account": {"id": "acct-target", "plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                },
            }
        }
        result = account_ops.check_account_status(
            self._credential(),
            requester=lambda _at, _proxy: (200, data),
        )
        self.assertEqual(result["status"], "free")

    def test_only_explicit_deactivation_is_banned(self):
        data = {
            "accounts": {
                "acct-target": {
                    "account": {"id": "acct-target", "is_deactivated": True},
                    "entitlement": {},
                }
            }
        }
        result = account_ops.check_account_status(
            self._credential(),
            requester=lambda _at, _proxy: (200, data),
        )
        self.assertEqual(result["status"], "banned")


class PageRouteTests(unittest.TestCase):
    def test_full_page_routes_exist(self):
        paths = {route.path for route in app.app.routes}
        self.assertIn("/pool", paths)
        self.assertIn("/accounts", paths)
        self.assertIn("/api/account-management/tasks/acquire-rt", paths)
        self.assertIn("/api/account-management/tasks/sub2-export", paths)
        self.assertTrue(str(app.pool_page().path).endswith("pool.html"))
        self.assertTrue(str(app.accounts_page().path).endswith("accounts.html"))


if __name__ == "__main__":
    unittest.main()
