from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from auth_flow import AuthResult
from webui import account_ops, app, db


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

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        with account_ops._lock:
            account_ops._tasks.clear()
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
            def __init__(self, _config):
                self.session = FakeSession()

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
        self.assertEqual(seen["password"], "mail-pass")
        source_call = provider.call_args.kwargs
        self.assertEqual(source_call["client_id"], "client-id")
        self.assertEqual(source_call["refresh_token"], "mail-refresh-token-long")
        stored = db.get_registered("person@example.com")
        self.assertEqual(stored["refresh_token"], "new-openai-rt")
        self.assertEqual(stored["access_token"], "original-web-at")


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
