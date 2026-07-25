from __future__ import annotations

import json
import logging
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import auth_flow as auth_flow_module
from auth_flow import AuthFlow, AuthResult, SignupInvalidStateError, SmsRequiredError
from mail_outlook import FatalOutlookMailError, OutlookMailProvider, fetch_otp_via_graph
from webui import app, db, registrar


class SseHeartbeatTests(unittest.TestCase):
    def test_queue_timeout_uses_proxy_safe_heartbeat_interval(self):
        class EmptyQueue:
            timeout = None

            def get(self, timeout):
                self.timeout = timeout
                raise queue.Empty

        source = EmptyQueue()
        self.assertIs(app._safe_get(source), app._SSE_HEARTBEAT)
        self.assertEqual(source.timeout, app.SSE_HEARTBEAT_SECONDS)
        self.assertLess(app.SSE_HEARTBEAT_SECONDS, 30)

    def test_heartbeat_is_comment_without_event_id(self):
        frame = app._sse_heartbeat_frame()
        self.assertEqual(frame, ": keep-alive\n\n")
        self.assertNotIn("id:", frame)
        self.assertEqual(
            app._sse_preamble(),
            f"retry: {app.SSE_RETRY_MILLISECONDS}\n: connected\n\n",
        )


class OutlookOtpFallbackTests(unittest.TestCase):
    def _provider(self):
        return OutlookMailProvider(
            email="otp-test@outlook.com",
            password="",
            client_id="client-id",
            refresh_token="refresh-token",
        )

    def test_graph_second_401_is_fatal(self):
        def unauthorized(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://graph.microsoft.com", 401, "Unauthorized", {}, None,
            )

        with (
            mock.patch(
                "mail_outlook._request_access_token",
                return_value={"access_token": "token"},
            ),
            mock.patch("mail_outlook._graph_list_messages", side_effect=unauthorized),
        ):
            with self.assertRaisesRegex(FatalOutlookMailError, "HTTP 401"):
                fetch_otp_via_graph(
                    "otp-test@outlook.com",
                    "refresh-token",
                    "client-id",
                    deadline=time.time() + 1,
                )

    def test_graph_auth_failure_falls_back_to_imap(self):
        with (
            mock.patch(
                "mail_outlook.fetch_otp_via_graph",
                side_effect=FatalOutlookMailError("Graph API 认证失败: HTTP 401"),
            ),
            mock.patch(
                "mail_outlook.fetch_otp_via_imap",
                return_value="123456",
            ) as imap,
        ):
            self.assertEqual(
                self._provider().wait_for_otp(
                    "otp-test@outlook.com", timeout=90, issued_after=time.time(),
                ),
                "123456",
            )

        self.assertTrue(imap.called)


class RunStreamTests(unittest.TestCase):
    def setUp(self):
        with registrar._lock:
            registrar._run_streams.clear()

    def tearDown(self):
        with registrar._lock:
            streams = list(registrar._run_streams.values())
            registrar._run_streams.clear()
        for stream in streams:
            if stream.journal_file is not None:
                stream.journal_file.close()

    def _create_stream(self, run_id: str, journal_path: Path) -> None:
        with registrar._lock:
            registrar._run_streams[run_id] = registrar.RunStream(
                journal_path=journal_path,
                journal_file=journal_path.open("w", encoding="utf-8"),
            )

    def test_history_broadcast_disconnect_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "run.events.jsonl"
            self._create_stream("run1", journal)

            registrar._publish_run_event("run1", "first")
            history_a, queue_a, finished_a = registrar.subscribe_run("run1")
            history_b, queue_b, finished_b = registrar.subscribe_run("run1")
            self.assertEqual(history_a, [(1, "first")])
            self.assertEqual(history_b, history_a)
            self.assertFalse(finished_a or finished_b)

            registrar._publish_run_event("run1", "second")
            self.assertEqual(queue_a.get_nowait(), (2, "second"))
            self.assertEqual(queue_b.get_nowait(), (2, "second"))

            registrar.unsubscribe_run("run1", queue_a)
            registrar._emit_status("run1", "phase", {"phase": "sms"})
            event_id, status = queue_b.get_nowait()
            self.assertEqual(event_id, 3)
            self.assertIn('"phase": "sms"', status)
            self.assertTrue(queue_a.empty())

            registrar._finish_run_stream("run1")
            self.assertEqual(queue_b.get_nowait(), (4, "__END__"))
            self.assertIsNone(queue_b.get_nowait())

            history, subscriber, finished = registrar.subscribe_run("run1", 2)
            self.assertIsNone(subscriber)
            self.assertTrue(finished)
            self.assertEqual([item[0] for item in history], [3, 4])

            records = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual([item["id"] for item in records], [1, 2, 3, 4])
            self.assertEqual(records[-1]["message"], "__END__")

    def test_persisted_journal_replays_after_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "saved.log"
            log_path.write_text("human log\n", encoding="utf-8")
            journal = log_path.with_suffix(".events.jsonl")
            journal.write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False)
                    for item in (
                        {"id": 1, "message": "first"},
                        {"id": 2, "message": '__EVENT__:{"kind":"done"}'},
                        {"id": 3, "message": "__END__"},
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                registrar.db,
                "get_run",
                return_value={"run_id": "saved", "status": "done", "log_path": str(log_path)},
            ):
                events, finished = registrar.get_persisted_run_events("saved", 1)
            self.assertTrue(finished)
            self.assertEqual(events, [
                (2, '__EVENT__:{"kind":"done"}'),
                (3, "__END__"),
            ])

    def test_handler_filters_other_registration_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "thread.log"
            journal = Path(tmp) / "thread.events.jsonl"
            self._create_stream("thread", journal)
            handler = registrar.QueueLogHandler("thread", log_path)
            handler.setFormatter(logging.Formatter("%(message)s"))

            handler.handle(logging.LogRecord("test", logging.INFO, "", 0, "mine", (), None))

            def emit_other():
                handler.handle(logging.LogRecord("test", logging.INFO, "", 0, "other", (), None))

            thread = threading.Thread(target=emit_other)
            thread.start()
            thread.join()
            handler.close()

            self.assertEqual(log_path.read_text(encoding="utf-8"), "mine\n")
            history, _, _ = registrar.subscribe_run("thread")
            self.assertEqual(history, [(1, "mine")])


class SmsProactiveTests(unittest.TestCase):
    def test_setting_defaults_and_round_trip(self):
        original_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            db.DB_PATH = Path(tmp) / "test.db"
            try:
                db.init_db()
                self.assertEqual(db.get_sms_config()["sms_proactive"], "0")
                self.assertFalse(db.get_sms_internal_config()["sms_proactive"])
                db.save_sms_config({"sms_enabled": "0", "sms_proactive": "1"})
                self.assertEqual(db.get_sms_config()["sms_proactive"], "0")
                db.save_sms_config({"sms_enabled": "1", "sms_proactive": "1"})
                self.assertEqual(db.get_sms_config()["sms_proactive"], "1")
                self.assertTrue(db.get_sms_internal_config()["sms_proactive"])
                db.save_sms_config({"sms_proactive": "0"})
                self.assertFalse(db.get_sms_internal_config()["sms_proactive"])
            finally:
                db.DB_PATH = original_path

    def _flow(self, callback, required, sms_result=None, sms_error=None):
        flow = AuthFlow.__new__(AuthFlow)
        flow._sms_callback = callback
        flow._sms_required = required
        flow._handle_add_phone_via_env = lambda _url: "env-result"
        if sms_error is not None:
            def fail(_url):
                raise sms_error
            flow._handle_add_phone_via_sms = fail
        else:
            flow._handle_add_phone_via_sms = lambda _url: sms_result
        return flow

    def test_required_mode_never_falls_back(self):
        class Controller:
            cleaned = False

            def cleanup(self):
                self.cleaned = True

        controller = Controller()
        flow = self._flow(controller, True, sms_error=RuntimeError("provider down"))
        with self.assertRaisesRegex(SmsRequiredError, "provider down"):
            flow._handle_add_phone_verification("/add-phone")
        self.assertTrue(controller.cleaned)

        missing = self._flow(None, True)
        with self.assertRaises(SmsRequiredError):
            missing._handle_add_phone_verification("/add-phone")

    def test_legacy_mode_keeps_fallback(self):
        class Controller:
            def cleanup(self):
                pass

        flow = self._flow(Controller(), False, sms_error=RuntimeError("provider down"))
        self.assertEqual(flow._handle_add_phone_verification("/add-phone"), "env-result")

    def test_required_mode_without_api_key_fails_before_registration(self):
        cfg = {
            "sms_enabled": True,
            "sms_proactive": True,
            "sms_api_key": "",
        }
        with self.assertRaisesRegex(SmsRequiredError, "API Key"):
            registrar._build_sms_callback("missing-key", cfg)

    def test_codex_direct_add_phone_propagates_required_error(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow._codex_rt_attempted = False
        flow._sms_callback = None
        flow._sms_required = True
        flow._env_flag = lambda _name, _default="0": False
        flow._build_codex_authorize = lambda: (
            "https://auth.openai.com/authorize",
            "state",
            "verifier",
            "https://localhost/callback",
            "client",
        )
        flow._follow_authorize_for_callback = lambda *_args: (
            "",
            "https://auth.openai.com/add-phone",
        )
        with self.assertRaises(SmsRequiredError):
            flow.oauth_codex_rt_exchange()


class AccountCooldownTests(unittest.TestCase):
    FIRST_EMAIL = "cooldown-first@outlook.com"
    SECOND_EMAIL = "cooldown-second@outlook.com"

    def setUp(self):
        self._original_db_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "cooldown.db"

        # Start from the legacy schema so init_db exercises ALTER TABLE migration.
        con = db._conn()
        con.execute("""
            CREATE TABLE outlook_accounts (
                email TEXT PRIMARY KEY,
                password TEXT,
                client_id TEXT,
                refresh_token TEXT,
                status TEXT NOT NULL DEFAULT 'available',
                imported_at REAL,
                claimed_at REAL,
                finished_at REAL,
                fail_reason TEXT
            )
        """)
        con.commit()
        con.close()
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    def _import_two_accounts(self):
        token = "r" * 32
        db.import_accounts(
            f"{self.FIRST_EMAIL}----pw----client----{token}\n"
            f"{self.SECOND_EMAIL}----pw----client----{token}2"
        )
        con = db._conn()
        con.execute(
            "UPDATE outlook_accounts SET imported_at=1 WHERE email=?",
            (self.FIRST_EMAIL,),
        )
        con.execute(
            "UPDATE outlook_accounts SET imported_at=2 WHERE email=?",
            (self.SECOND_EMAIL,),
        )
        con.commit()
        con.close()

    def test_init_db_migrates_retry_after_column(self):
        con = db._conn()
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(outlook_accounts)").fetchall()
        }
        con.close()
        self.assertIn("retry_after", columns)

    def test_cooldown_account_stays_available_and_claim_next_skips_it(self):
        self._import_two_accounts()
        first = db.claim_next()
        self.assertEqual(first["email"], self.FIRST_EMAIL)

        before_release = time.time()
        db.release_unused(self.FIRST_EMAIL, cooldown_seconds=300)
        cooled = db.get_account(self.FIRST_EMAIL)

        self.assertEqual(cooled["status"], "available")
        self.assertGreaterEqual(cooled["retry_after"], before_release + 299)
        second = db.claim_next()
        self.assertEqual(second["email"], self.SECOND_EMAIL)

    def test_explicit_claim_ignores_cooldown_and_clears_retry_after(self):
        self._import_two_accounts()
        db.claim_next()
        db.release_unused(self.FIRST_EMAIL, cooldown_seconds=300)

        claimed = db.claim_account(self.FIRST_EMAIL)

        self.assertIsNotNone(claimed)
        stored = db.get_account(self.FIRST_EMAIL)
        self.assertEqual(stored["status"], "in_use")
        self.assertIsNone(stored["retry_after"])

    def test_reset_clears_cooldown(self):
        self._import_two_accounts()
        db.claim_next()
        db.release_unused(self.FIRST_EMAIL, cooldown_seconds=300)

        self.assertTrue(db.reset_to_available(self.FIRST_EMAIL))
        reset = db.get_account(self.FIRST_EMAIL)
        self.assertEqual(reset["status"], "available")
        self.assertIsNone(reset["retry_after"])
        self.assertEqual(db.claim_next()["email"], self.FIRST_EMAIL)


class SignupInvalidStateRetryTests(unittest.TestCase):
    EMAIL = "signup-retry@outlook.com"
    INVALID_STATE_MESSAGE = (
        "authorize/continue 失败(screen_hint=signup): HTTP 409 req_id=req-409 body="
        '{"error":{"message":"Your sign-in session is no longer valid. '
        'Please start over to continue.","code":"invalid_state"}}'
    )

    @staticmethod
    def _result(
        *,
        email: str = EMAIL,
        access_token: str = "",
        session_token: str = "",
        refresh_token: str = "",
    ) -> AuthResult:
        result = AuthResult()
        result.email = email
        result.password = "signup-retryoutlook.com"
        result.access_token = access_token
        result.session_token = session_token
        result.refresh_token = refresh_token
        return result

    def _invalid_state_error(self) -> RuntimeError:
        error_cls = getattr(auth_flow_module, "SignupInvalidStateError", None)
        if error_cls is None:
            error_cls = type("MissingSignupInvalidStateError", (RuntimeError,), {})
        return error_cls(self.INVALID_STATE_MESSAGE)

    def _run_registrar(
        self,
        flows: list[mock.Mock],
        *,
        mail_source: str = "outlook",
        options: dict | None = None,
    ) -> dict:
        account = {
            "email": self.EMAIL,
            "password": "mail-password",
            "client_id": "client-id",
            "refresh_token": "mail-refresh-token",
        }
        run_options = options or {
            "want_access_token": True,
            "want_session_token": True,
            "want_refresh_token": True,
            "otp_timeout": 180,
        }
        settings = {
            "mail_source": mail_source,
            "cf_api_url": "https://mail.example.com",
            "cf_domain": "example.com",
        }
        mail = mock.Mock(name=f"{mail_source}_mail_provider")
        cfg = mock.Mock(name="config")

        def get_setting(key, default=""):
            return settings.get(key, default)

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(registrar.db, "get_setting", side_effect=get_setting),
                mock.patch.object(
                    registrar.db,
                    "get_sms_internal_config",
                    return_value={"sms_enabled": False, "sms_proactive": False},
                ),
                mock.patch.object(registrar.db, "get_cf_admin_token", return_value="admin-token"),
                mock.patch.object(registrar.db, "save_registered") as save_registered,
                mock.patch.object(registrar.db, "mark_done") as mark_done,
                mock.patch.object(registrar.db, "mark_failed") as mark_failed,
                mock.patch.object(registrar.db, "release_unused") as release_unused,
                mock.patch.object(registrar.db, "finish_run") as finish_run,
                mock.patch.object(registrar.db, "claim_next") as claim_next,
                mock.patch.object(registrar, "Config", return_value=cfg),
                mock.patch.object(registrar, "_build_sms_callback", return_value=None),
                mock.patch.object(registrar, "_emit_status") as emit_status,
                mock.patch.object(registrar, "_try_export_to_panels"),
                mock.patch.object(registrar, "_finish_run_stream"),
                mock.patch.object(registrar, "AuthFlow", side_effect=flows) as flow_cls,
                mock.patch.object(
                    registrar,
                    "OutlookMailProvider",
                    return_value=mail,
                ) as outlook_provider_cls,
                mock.patch("mail_cf.CFTempEmailProvider", return_value=mail) as cf_provider_cls,
            ):
                registrar._do_register(
                    "signup-retry-run",
                    account,
                    run_options,
                    Path(tmp) / "signup-retry.log",
                )

        return {
            "mail": mail,
            "cfg": cfg,
            "flow_cls": flow_cls,
            "outlook_provider_cls": outlook_provider_cls,
            "cf_provider_cls": cf_provider_cls,
            "save_registered": save_registered,
            "mark_done": mark_done,
            "mark_failed": mark_failed,
            "release_unused": release_unused,
            "finish_run": finish_run,
            "claim_next": claim_next,
            "emit_status": emit_status,
        }

    def test_signup_409_invalid_state_becomes_dedicated_error(self):
        error_cls = getattr(auth_flow_module, "SignupInvalidStateError", None)
        self.assertIsNotNone(
            error_cls,
            "auth_flow must expose SignupInvalidStateError",
        )
        self.assertTrue(issubclass(error_cls, RuntimeError))

        original = RuntimeError(self.INVALID_STATE_MESSAGE)
        flow = AuthFlow.__new__(AuthFlow)
        flow.authorize_continue = mock.Mock(side_effect=original)

        with self.assertRaises(error_cls) as raised:
            flow.signup(self.EMAIL, "sentinel-token")

        self.assertIs(raised.exception.__cause__, original)

    def test_signup_does_not_convert_other_conflicts(self):
        original = RuntimeError(
            'authorize/continue 失败(screen_hint=signup): HTTP 409 body={"code":"other"}'
        )
        flow = AuthFlow.__new__(AuthFlow)
        flow.authorize_continue = mock.Mock(side_effect=original)

        with self.assertRaises(RuntimeError) as raised:
            flow.signup(self.EMAIL, "sentinel-token")

        self.assertIs(raised.exception, original)

    def test_outlook_uses_one_fresh_flow_retry_and_same_mail_provider(self):
        first = mock.Mock(name="first_flow")
        first.result = self._result()
        first.run_register.side_effect = self._invalid_state_error()

        recovered = self._result(
            access_token="second-access",
            session_token="second-session",
            refresh_token="second-refresh",
        )
        second = mock.Mock(name="second_flow")
        second.result = recovered
        second.run_register.return_value = recovered

        state = self._run_registrar([first, second])

        self.assertEqual(state["flow_cls"].call_count, 2)
        first.run_register.assert_called_once_with(state["mail"])
        second.run_register.assert_called_once_with(state["mail"])
        state["outlook_provider_cls"].assert_called_once()
        state["cf_provider_cls"].assert_not_called()
        state["claim_next"].assert_not_called()
        saved = state["save_registered"].call_args.args[0]
        self.assertEqual(saved["email"], self.EMAIL)
        self.assertEqual(saved["access_token"], "second-access")
        self.assertEqual(saved["session_token"], "second-session")
        self.assertEqual(saved["refresh_token"], "second-refresh")
        state["mark_done"].assert_called_once_with(self.EMAIL)

    def test_outlook_stops_after_second_signup_invalid_state(self):
        first = mock.Mock(name="first_flow")
        first.result = self._result()
        first.run_register.side_effect = self._invalid_state_error()
        second = mock.Mock(name="second_flow")
        second.result = self._result()
        second.run_register.side_effect = self._invalid_state_error()

        state = self._run_registrar([first, second])

        self.assertEqual(state["flow_cls"].call_count, 2)
        first.run_register.assert_called_once_with(state["mail"])
        second.run_register.assert_called_once_with(state["mail"])
        state["save_registered"].assert_not_called()
        state["release_unused"].assert_called_once_with(
            self.EMAIL,
            cooldown_seconds=300,
        )
        state["mark_failed"].assert_not_called()
        state["finish_run"].assert_called_once()
        self.assertEqual(state["finish_run"].call_args.args[1], "failed")
        self.assertEqual(state["finish_run"].call_args.kwargs["category"], "network")

    def test_second_flow_late_partial_uses_second_result(self):
        first = mock.Mock(name="first_flow")
        first.result = self._result(access_token="stale-first-access")
        first.run_register.side_effect = self._invalid_state_error()

        second = mock.Mock(name="second_flow")
        second.result = self._result(access_token="second-partial-access")
        second.run_register.side_effect = RuntimeError("late callback failure")

        state = self._run_registrar(
            [first, second],
            options={
                "want_access_token": True,
                "want_session_token": True,
                "want_refresh_token": False,
                "otp_timeout": 180,
            },
        )

        self.assertEqual(state["flow_cls"].call_count, 2)
        saved = state["save_registered"].call_args.args[0]
        self.assertEqual(saved["access_token"], "second-partial-access")
        self.assertNotEqual(saved["access_token"], "stale-first-access")
        done_events = [
            call for call in state["emit_status"].call_args_list
            if len(call.args) >= 2 and call.args[1] == "done"
        ]
        self.assertEqual(len(done_events), 1)
        self.assertTrue(done_events[0].args[2]["partial"])

    def test_other_runtime_error_does_not_retry(self):
        first = mock.Mock(name="first_flow")
        first.result = self._result()
        first.run_register.side_effect = RuntimeError("upstream unavailable")

        state = self._run_registrar([first])

        state["flow_cls"].assert_called_once()
        first.run_register.assert_called_once_with(state["mail"])
        state["save_registered"].assert_not_called()

    def test_cf_mail_does_not_retry_signup_invalid_state(self):
        first = mock.Mock(name="cf_flow")
        first.result = self._result(email="first-random@example.com")
        first.run_register.side_effect = self._invalid_state_error()

        state = self._run_registrar([first], mail_source="cf_temp")

        state["flow_cls"].assert_called_once()
        first.run_register.assert_called_once_with(state["mail"])
        state["cf_provider_cls"].assert_called_once_with(
            api_url="https://mail.example.com",
            admin_token="admin-token",
            domain="example.com",
        )
        state["outlook_provider_cls"].assert_not_called()
        state["save_registered"].assert_not_called()


class ExistingAccountFreshLoginTests(unittest.TestCase):
    EMAIL = "existing-account@outlook.com"
    PASSWORD = "existing-accountoutlook.com"

    def _signup_flow(self, create_error: RuntimeError):
        flow = AuthFlow.__new__(AuthFlow)
        flow.config = object()
        flow._sms_callback = object()
        flow._sms_required = True
        flow._is_existing_account = False
        flow._existing_email_verification_mode = ""
        flow._existing_page_type = ""
        flow.result = AuthResult()
        flow.result.password = self.PASSWORD

        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf")
        flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel")
        flow.signup = mock.Mock(return_value=True)
        flow.register_password = mock.Mock(return_value=True)
        flow.send_otp = mock.Mock()
        flow.verify_otp = mock.Mock(return_value={})
        flow.fetch_client_auth_session_dump = mock.Mock(return_value={})
        flow.create_account = mock.Mock(side_effect=create_error)

        mail = mock.Mock()
        mail.create_mailbox.return_value = self.EMAIL
        mail.wait_for_otp.return_value = "123456"
        return flow, mail

    def _passwordless_otp_flow(
        self,
        *,
        mode="passwordless_signup",
        create_result=None,
        create_error=None,
        verify_continue_url="https://auth.openai.com/about-you",
    ):
        flow = AuthFlow.__new__(AuthFlow)
        flow.config = object()
        flow._sms_callback = object()
        flow._sms_required = False
        flow._is_existing_account = True
        flow._existing_email_verification_mode = mode
        flow._existing_page_type = "email_otp_verification"
        flow.result = AuthResult()

        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf")
        flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel")
        flow.signup = mock.Mock(return_value=False)
        flow.register_password = mock.Mock()
        flow.send_otp = mock.Mock()
        flow.kickoff_otp_delivery = mock.Mock(return_value=True)
        flow.verify_otp = mock.Mock(return_value={
            "continue_url": verify_continue_url,
        })
        flow.fetch_client_auth_session_dump = mock.Mock(return_value={})
        if create_error is not None:
            flow.create_account = mock.Mock(side_effect=create_error)
        else:
            flow.create_account = mock.Mock(return_value=create_result)
        flow._normalize_continue_url = mock.Mock(side_effect=lambda url: url)
        flow._is_add_phone_state = mock.Mock(return_value=False)
        flow._env_flag = mock.Mock(return_value=False)
        flow.follow_redirect_chain = mock.Mock(return_value=(
            "https://chatgpt.com/api/auth/callback/openai?code=callback-code&state=state",
            "https://chatgpt.com/",
        ))
        flow._consume_callback_for_session = mock.Mock(return_value=True)
        flow.oauth_codex_rt_exchange = mock.Mock(return_value=False)
        flow.oauth_token_exchange = mock.Mock(return_value=False)
        flow.oauth_secondary_authorize_exchange = mock.Mock(return_value=False)

        def set_session_tokens():
            flow.result.access_token = "registered-access-token"
            flow.result.session_token = "registered-session-token"
            return flow.result.session_token, flow.result.access_token

        flow.get_auth_session = mock.Mock(side_effect=set_session_tokens)

        mail = mock.Mock()
        mail.create_mailbox.return_value = self.EMAIL
        mail.wait_for_otp.return_value = "123456"
        mail._outlook_creds = ["outlook-creds"]
        return flow, mail

    def test_signup_user_already_exists_uses_one_fresh_login_and_returns_tokens(self):
        conflict = RuntimeError(
            '创建账户失败: 400 - {"error":{"code":"user_already_exists"},'
            '"redirect_uri":"https://chatgpt.com/auth/login_with?callback_path=/"}'
        )
        flow, mail = self._signup_flow(conflict)
        recovered = AuthResult()
        recovered.email = self.EMAIL
        recovered.access_token = "fresh-access-token"
        recovered.session_token = "fresh-session-token"

        fresh_flow = mock.Mock()
        fresh_flow.run_protocol_login.return_value = recovered
        with mock.patch("auth_flow.AuthFlow", return_value=fresh_flow) as flow_cls:
            result = flow.run_register(mail)

        self.assertIs(result, recovered)
        self.assertEqual(result.access_token, "fresh-access-token")
        self.assertEqual(result.session_token, "fresh-session-token")
        flow_cls.assert_called_once_with(
            flow.config,
            sms_callback=flow._sms_callback,
            sms_required=flow._sms_required,
        )
        fresh_flow.run_protocol_login.assert_called_once_with(
            mail,
            self.EMAIL,
            password=self.PASSWORD,
            allow_registration_fallback=False,
        )

    def test_known_existing_account_continues_current_otp_transaction(self):
        flow, mail = self._passwordless_otp_flow(
            mode="passwordless_login",
            verify_continue_url="https://auth.openai.com/continue-after-login-otp",
        )

        with (
            mock.patch.dict("os.environ", {"WEBUI_ALLOW_LOGIN": "1"}),
            mock.patch("auth_flow.AuthFlow") as flow_cls,
        ):
            result = flow.run_register(mail)

        self.assertIs(result, flow.result)
        self.assertTrue(result.is_valid())
        flow_cls.assert_not_called()
        flow.register_password.assert_not_called()
        flow.send_otp.assert_not_called()
        mail.wait_for_otp.assert_called_once()
        flow.verify_otp.assert_called_once_with("123456")
        flow.create_account.assert_not_called()

    def test_passwordless_signup_completes_original_registration_without_fresh_login(self):
        flow, mail = self._passwordless_otp_flow(
            create_result="https://auth.openai.com/continue-after-about-you",
        )

        with (
            mock.patch.dict("os.environ", {"WEBUI_ALLOW_LOGIN": "1"}),
            mock.patch("auth_flow.AuthFlow") as flow_cls,
        ):
            result = flow.run_register(mail)

        self.assertIs(result, flow.result)
        self.assertEqual(result.access_token, "registered-access-token")
        self.assertEqual(result.session_token, "registered-session-token")
        flow_cls.assert_not_called()
        flow.register_password.assert_not_called()
        flow.send_otp.assert_not_called()
        mail.wait_for_otp.assert_called_once()
        flow.verify_otp.assert_called_once_with("123456")
        flow.create_account.assert_called_once_with()

    def test_passwordless_signup_user_already_exists_uses_fresh_login(self):
        conflict = RuntimeError(
            '创建账户失败: 400 - {"error":{"code":"user_already_exists"},'
            '"redirect_uri":"https://chatgpt.com/auth/login_with?callback_path=/"}'
        )
        flow, mail = self._passwordless_otp_flow(create_error=conflict)
        recovered = AuthResult()
        recovered.email = self.EMAIL
        recovered.access_token = "fresh-access-token"
        recovered.session_token = "fresh-session-token"
        fresh_flow = mock.Mock()
        fresh_flow.run_protocol_login.return_value = recovered

        with (
            mock.patch.dict("os.environ", {"WEBUI_ALLOW_LOGIN": "1"}),
            mock.patch("auth_flow.AuthFlow", return_value=fresh_flow) as flow_cls,
        ):
            result = flow.run_register(mail)

        self.assertIs(result, recovered)
        mail.wait_for_otp.assert_called_once()
        flow.verify_otp.assert_called_once_with("123456")
        flow.create_account.assert_called_once_with()
        flow_cls.assert_called_once_with(
            flow.config,
            sms_callback=flow._sms_callback,
            sms_required=flow._sms_required,
        )
        fresh_flow.run_protocol_login.assert_called_once_with(
            mail,
            self.EMAIL,
            password="",
            allow_registration_fallback=False,
        )

    def test_other_create_account_error_is_raised_without_fresh_login(self):
        create_error = RuntimeError("创建账户失败: 500 - upstream unavailable")
        flow, mail = self._signup_flow(create_error)

        with mock.patch("auth_flow.AuthFlow") as flow_cls:
            with self.assertRaises(RuntimeError) as raised:
                flow.run_register(mail)

        self.assertIs(raised.exception, create_error)
        flow_cls.assert_not_called()

    def test_fresh_protocol_login_cannot_fall_back_to_registration(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf")
        flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel")
        flow.signup = mock.Mock(return_value=True)
        flow.register_password = mock.Mock()
        flow.create_account = mock.Mock()
        mail = mock.Mock()

        with mock.patch.dict(
            "os.environ", {"LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT": "0"}
        ):
            with self.assertRaises(RuntimeError):
                flow.run_protocol_login(
                    mail,
                    self.EMAIL,
                    password=self.PASSWORD,
                    allow_registration_fallback=False,
                )

        flow.register_password.assert_not_called()
        flow.create_account.assert_not_called()

    def test_protocol_login_invalid_state_stops_before_signup_fallback(self):
        invalid_state = RuntimeError(
            "authorize/continue 失败(screen_hint=login): "
            'HTTP 409 body={"code":"invalid_state"}'
        )
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf")
        flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel")
        flow.authorize_continue = mock.Mock(side_effect=invalid_state)
        flow.signup = mock.Mock()
        mail = mock.Mock()

        with self.assertRaises(SignupInvalidStateError) as raised:
            flow.run_protocol_login(
                mail,
                self.EMAIL,
                password=self.PASSWORD,
                allow_registration_fallback=False,
            )

        self.assertIs(raised.exception.__cause__, invalid_state)
        flow.signup.assert_not_called()

    def test_protocol_login_other_409_keeps_normal_fallback(self):
        other_conflict = RuntimeError(
            "authorize/continue 失败(screen_hint=login): "
            'HTTP 409 body={"code":"other_conflict"}'
        )
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf")
        flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel")
        flow.authorize_continue = mock.Mock(side_effect=other_conflict)
        flow.signup = mock.Mock(return_value=True)
        mail = mock.Mock()

        with self.assertRaises(RuntimeError) as raised:
            flow.run_protocol_login(
                mail,
                self.EMAIL,
                password=self.PASSWORD,
                allow_registration_fallback=False,
            )

        self.assertNotIsInstance(raised.exception, SignupInvalidStateError)
        flow.signup.assert_called_once_with(self.EMAIL, "sentinel")

    def test_existing_account_wrapper_preserves_invalid_state_for_retry(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.config = object()
        flow._sms_callback = object()
        flow._sms_required = False
        flow.result = AuthResult()
        flow.result.password = self.PASSWORD
        mail = mock.Mock()

        invalid_state = SignupInvalidStateError(
            "authorize/continue 失败(screen_hint=login): "
            'HTTP 409 body={"code":"invalid_state"}'
        )
        login_flow = mock.Mock()
        login_flow.result = AuthResult()
        login_flow.run_protocol_login.side_effect = invalid_state

        with mock.patch("auth_flow.AuthFlow", return_value=login_flow):
            with self.assertRaises(SignupInvalidStateError) as raised:
                flow._run_existing_account_login(mail, self.EMAIL)

        self.assertIs(raised.exception, invalid_state)
        login_flow.session.close.assert_called_once_with()


class ProtocolLoginCallbackRecoveryTests(unittest.TestCase):
    EMAIL = "callback-recovery@outlook.com"
    AUTH_URL = "https://auth.openai.com/oauth/authorize?state=original-state"
    AFTER_OTP_URL = "https://auth.openai.com/after-otp"
    CALLBACK_URL = (
        "https://chatgpt.com/api/auth/callback/openai"
        "?code=callback-code&state=original-state"
    )
    FINAL_URL = "https://chatgpt.com/"
    WORKSPACE_URL = "https://auth.openai.com/workspace?id=workspace-id"
    AFTER_WORKSPACE_URL = "https://auth.openai.com/after-workspace"

    def _flow(
        self,
        *,
        redirects: list[tuple[str, str]],
        reauthorize_callback: str = "",
        session_valid_after: int = 1,
    ) -> tuple[AuthFlow, mock.Mock, list[tuple[str, str]]]:
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow._is_existing_account = False
        flow._existing_email_verification_mode = ""
        flow._existing_page_type = ""

        flow.check_proxy = mock.Mock(return_value=True)
        flow.get_csrf_token = mock.Mock(return_value="csrf-token")
        flow.get_auth_url = mock.Mock(return_value=self.AUTH_URL)
        flow.auth_oauth_init = mock.Mock(return_value="device-id")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel-token")
        flow.authorize_continue = mock.Mock(return_value={
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_login"},
            },
            "continue_url": "https://auth.openai.com/email-verification",
        })
        flow.signup = mock.Mock()
        flow.login_password_verify = mock.Mock()
        flow.register_password = mock.Mock()
        flow.send_otp = mock.Mock()
        flow.kickoff_otp_delivery = mock.Mock(return_value=True)
        flow.verify_otp = mock.Mock(return_value={
            "page": {"type": "redirect"},
            "continue_url": self.AFTER_OTP_URL,
        })
        flow.fetch_client_auth_session_dump = mock.Mock(return_value={})
        flow._is_add_phone_state = mock.Mock(return_value=False)
        flow._handle_add_phone_verification = mock.Mock()

        def normalize(url: str) -> str:
            if url == self.WORKSPACE_URL:
                return self.AFTER_WORKSPACE_URL
            return url

        flow._normalize_continue_url = mock.Mock(side_effect=normalize)
        flow.follow_redirect_chain = mock.Mock(side_effect=redirects)
        flow._reauthorize_for_session = mock.Mock(return_value=reauthorize_callback)
        flow.oauth_token_exchange = mock.Mock(return_value=False)
        flow.oauth_codex_rt_exchange = mock.Mock(return_value=False)
        flow.oauth_secondary_authorize_exchange = mock.Mock(return_value=False)
        flow._env_flag = mock.Mock(return_value=False)

        events: list[tuple[str, str]] = []

        def consume_callback(callback_url: str) -> bool:
            events.append(("consume", callback_url))
            return True

        def get_auth_session() -> tuple[str, str]:
            events.append(("session", ""))
            if len([event for event in events if event[0] == "session"]) >= session_valid_after:
                flow.result.session_token = "session-token"
                flow.result.access_token = "access-token"
            return flow.result.session_token, flow.result.access_token

        flow._consume_callback_for_session = mock.Mock(side_effect=consume_callback)
        flow.get_auth_session = mock.Mock(side_effect=get_auth_session)

        mail = mock.Mock(name="outlook_mail_provider")
        mail.wait_for_otp.return_value = "123456"
        return flow, mail, events

    def _run(self, flow: AuthFlow, mail: mock.Mock) -> AuthResult:
        with mock.patch.dict(
            "os.environ",
            {
                "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT": "1",
                "OTP_TIMEOUT": "60",
            },
        ):
            return flow.run_protocol_login(
                mail,
                self.EMAIL,
                password="account-password",
                allow_registration_fallback=False,
            )

    def test_empty_initial_callback_reauthorizes_and_uses_recovered_callback(self):
        flow, mail, events = self._flow(
            redirects=[("", self.FINAL_URL)],
            reauthorize_callback=self.CALLBACK_URL,
        )

        result = self._run(flow, mail)

        self.assertTrue(result.is_valid())
        flow.follow_redirect_chain.assert_called_once_with(self.AFTER_OTP_URL)
        flow._reauthorize_for_session.assert_called_once_with(self.AUTH_URL)
        flow._consume_callback_for_session.assert_called_once_with(self.CALLBACK_URL)
        flow.oauth_token_exchange.assert_not_called()
        self.assertEqual(
            events[:2],
            [
                ("consume", self.CALLBACK_URL),
                ("session", ""),
            ],
        )

    def test_non_refresh_only_consumes_callback_before_first_session_fetch(self):
        flow, mail, events = self._flow(
            redirects=[(self.CALLBACK_URL, self.FINAL_URL)],
        )

        result = self._run(flow, mail)

        self.assertTrue(result.is_valid())
        flow._reauthorize_for_session.assert_not_called()
        flow._consume_callback_for_session.assert_called_once_with(self.CALLBACK_URL)
        self.assertGreaterEqual(flow.get_auth_session.call_count, 1)
        self.assertEqual(events[0], ("consume", self.CALLBACK_URL))
        self.assertEqual(events[1][0], "session")
        flow.signup.assert_not_called()
        flow.register_password.assert_not_called()
        flow.send_otp.assert_not_called()

    def test_workspace_final_url_still_runs_workspace_fallback_before_reauthorize(self):
        flow, mail, events = self._flow(
            redirects=[
                ("", self.WORKSPACE_URL),
                (self.CALLBACK_URL, self.FINAL_URL),
            ],
        )

        result = self._run(flow, mail)

        self.assertTrue(result.is_valid())
        self.assertEqual(
            flow.follow_redirect_chain.call_args_list,
            [
                mock.call(self.AFTER_OTP_URL),
                mock.call(self.AFTER_WORKSPACE_URL),
            ],
        )
        flow._normalize_continue_url.assert_any_call(self.WORKSPACE_URL)
        flow._reauthorize_for_session.assert_not_called()
        flow._consume_callback_for_session.assert_called_once_with(self.CALLBACK_URL)
        self.assertEqual(events[0], ("consume", self.CALLBACK_URL))

    def test_stale_callback_reauthorizes_once_and_retries_session(self):
        stale_callback = (
            "https://chatgpt.com/api/auth/callback/openai"
            "?code=stale-code&state=original-state"
        )
        flow, mail, events = self._flow(
            redirects=[(stale_callback, self.FINAL_URL)],
            reauthorize_callback=self.CALLBACK_URL,
            session_valid_after=2,
        )

        result = self._run(flow, mail)

        self.assertTrue(result.is_valid())
        flow._reauthorize_for_session.assert_called_once_with(self.AUTH_URL)
        self.assertEqual(
            flow._consume_callback_for_session.call_args_list,
            [
                mock.call(stale_callback),
                mock.call(self.CALLBACK_URL),
            ],
        )
        self.assertEqual(
            events[:4],
            [
                ("consume", stale_callback),
                ("session", ""),
                ("consume", self.CALLBACK_URL),
                ("session", ""),
            ],
        )

    def test_redirect_chain_does_not_consume_callback_start_url(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.session = mock.Mock()
        flow._sniff_login_verifier = mock.Mock()

        callback_url, final_url = flow.follow_redirect_chain(self.CALLBACK_URL)

        self.assertEqual(callback_url, self.CALLBACK_URL)
        self.assertEqual(final_url, self.CALLBACK_URL)
        flow.session.get.assert_not_called()
        flow._sniff_login_verifier.assert_called_once()


if __name__ == "__main__":
    unittest.main()
