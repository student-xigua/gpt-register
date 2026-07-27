from __future__ import annotations

import gc
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from webui import account_ops, db, link_gen


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def kakao_page(amount: int, *, session_id: str) -> dict:
    return {
        "currency": "krw",
        "payment_method_types": ["card", "kakao_pay"],
        "ordered_payment_method_types": ["kakao_pay"],
        "elements_options": {"amount": amount},
        "elements_session_id": session_id,
        "config_id": "cfg_test",
        "init_checksum": "checksum_test",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
    }


class KakaoHttpScenario:
    def __init__(self, *, post_promo_amount=0, confirm_requires_approval=False, poll_timeouts=0):
        self.post_promo_amount = post_promo_amount
        self.confirm_requires_approval = confirm_requires_approval
        self.poll_timeouts = poll_timeouts
        self.init_count = 0
        self.calls: list[tuple[str, str, dict]] = []
        self.sessions: list[FakeSession] = []

    def session(self, impersonate="chrome"):
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def post(self, url, kwargs):
        self.calls.append(("POST", url, kwargs))
        if url == link_gen.OAI_CHECKOUT:
            return FakeResponse(payload={
                "checkout_session_id": "cs_test",
                "publishable_key": "pk_test",
                "processor_entity": "openai_llc",
                "client_secret": "cs_test_secret_demo",
                "requires_manual_approval": self.confirm_requires_approval,
            })
        if url == link_gen.OAI_UPDATE:
            return FakeResponse(payload={"success": True})
        if url == link_gen.OAI_TAXES:
            return FakeResponse(payload={"success": True})
        if url.endswith("/payment_pages/cs_test/init"):
            self.init_count += 1
            if self.init_count == 1:
                return FakeResponse(payload=kakao_page(12000, session_id="es_bootstrap"))
            return FakeResponse(payload=kakao_page(self.post_promo_amount, session_id=f"es_{self.init_count}"))
        if url.endswith("/payment_pages/cs_test/pre_confirm"):
            return FakeResponse(payload={"ok": True})
        if url.endswith("/payment_methods"):
            return FakeResponse(payload={"id": "pm_kakao_test"})
        if url.endswith("/payment_pages/cs_test/confirm"):
            if self.confirm_requires_approval:
                return FakeResponse(payload={"submission_attempt": {"state": "requires_approval"}})
            return FakeResponse(payload={
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://hooks.stripe.com/kakao/start"},
                }
            })
        if url == link_gen.OAI_APPROVE:
            return FakeResponse(payload={"result": "approved"})
        if url.endswith("/payment_pages/cs_test"):
            return FakeResponse(payload=kakao_page(0, session_id="es_tax"))
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, kwargs):
        self.calls.append(("GET", url, kwargs))
        if url in {
            "https://pay.openai.com/c/pay/cs_test",
            "https://checkout.stripe.com/c/pay/cs_test",
        }:
            return FakeResponse()
        if url == "https://api.stripe.com/v1/payment_pages/cs_test":
            if self.poll_timeouts:
                self.poll_timeouts -= 1
                raise TimeoutError("transient Stripe poll timeout")
            return FakeResponse(payload={
                "payment_intent": {
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {"url": "https://hooks.stripe.com/kakao/poll"},
                    }
                }
            })
        if url.startswith("https://hooks.stripe.com/kakao/"):
            return FakeResponse(
                status_code=302,
                headers={"Location": "https://web.nicepay.co.kr/kakao/checkout"},
            )
        raise AssertionError(f"unexpected GET {url}")


class FakeSession:
    def __init__(self, scenario: KakaoHttpScenario):
        self.scenario = scenario
        self.closed = False

    def post(self, url, **kwargs):
        return self.scenario.post(url, kwargs)

    def get(self, url, **kwargs):
        return self.scenario.get(url, kwargs)

    def close(self):
        self.closed = True


class FakeCffi:
    def __init__(self, scenario: KakaoHttpScenario):
        self.scenario = scenario

    def Session(self, impersonate="chrome"):
        return self.scenario.session(impersonate)


class KakaoLinkFlowTests(unittest.TestCase):
    checkout_seed = "http://user-region-us:pass@checkout.example:8000"
    promotion_seed = "http://user-region-br:pass@promotion.example:8000"

    def run_flow(self, scenario: KakaoHttpScenario, **kwargs):
        preflight_calls = []
        checkout_proxy = kwargs.pop("checkout_proxy", self.checkout_seed)
        update_proxy = kwargs.pop("update_proxy", self.promotion_seed)

        def preflight(_cffi, proxy, expected_country, label):
            preflight_calls.append((proxy, expected_country, label))

        with (
            mock.patch.object(link_gen, "_import_cffi", return_value=FakeCffi(scenario)),
            mock.patch.object(link_gen, "_preflight_kakao_proxy", side_effect=preflight),
        ):
            result = link_gen.generate_link(
                "access-token",
                "kakao",
                checkout_proxy=checkout_proxy,
                update_proxy=update_proxy,
                checkout_pool=[self.checkout_seed, "http://other-region-us:pass@other.example:8000"],
                poll_seconds=5,
                approve_workers=2,
                **kwargs,
            )
        return result, preflight_calls

    def test_full_kakao_flow_syncs_taxes_preconfirms_and_follows_nicepay(self):
        scenario = KakaoHttpScenario()
        result, preflight = self.run_flow(scenario)

        self.assertTrue(result["ok"])
        self.assertEqual(len(scenario.sessions), 3)
        self.assertTrue(all(session.closed for session in scenario.sessions))
        self.assertEqual(result["stripe_redirect_url"], "https://hooks.stripe.com/kakao/start")
        self.assertEqual(result["link"], "https://web.nicepay.co.kr/kakao/checkout")
        self.assertEqual(result["provider_redirect_url"], result["link"])
        self.assertEqual(
            [(country, label) for _, country, label in preflight],
            [("KR", "KR checkout/provider"), ("VN", "VN promotion")],
        )

        checkout_call = next(call for call in scenario.calls if call[1] == link_gen.OAI_CHECKOUT)
        update_call = next(call for call in scenario.calls if call[1] == link_gen.OAI_UPDATE)
        tax_call = next(call for call in scenario.calls if call[1] == link_gen.OAI_TAXES)
        pre_confirm_call = next(call for call in scenario.calls if call[1].endswith("/pre_confirm"))
        stripe_update_call = next(
            call for call in scenario.calls
            if call[1] == f"{link_gen.STRIPE_API}/payment_pages/cs_test"
        )
        bootstrap_init_call = next(call for call in scenario.calls if call[1].endswith("/init"))
        payment_method_call = next(call for call in scenario.calls if call[1].endswith("/payment_methods"))
        confirm_call = next(call for call in scenario.calls if call[1].endswith("/confirm"))

        self.assertEqual(checkout_call[2]["json"]["checkout_ui_mode"], "custom")
        self.assertEqual(checkout_call[2]["json"]["cancel_url"], "https://chatgpt.com/#pricing")
        self.assertIn("region-kr", checkout_call[2]["proxies"]["https"])
        self.assertIn("region-vn", update_call[2]["proxies"]["https"])
        self.assertIn("region-kr", tax_call[2]["proxies"]["https"])
        self.assertEqual(pre_confirm_call[2]["data"]["payment_method_type"], "kakao_pay")
        self.assertEqual(
            stripe_update_call[2]["data"]["elements_session_client[stripe_js_id]"],
            bootstrap_init_call[2]["data"]["elements_session_client[stripe_js_id]"],
        )
        self.assertEqual(
            bootstrap_init_call[2]["data"]["elements_session_client[locale]"],
            "ko",
        )
        self.assertEqual(confirm_call[2]["data"]["expected_amount"], "0")
        self.assertEqual(confirm_call[2]["data"]["expected_payment_method_type"], "kakao_pay")
        self.assertEqual(confirm_call[2]["data"]["eid"], "NA")
        self.assertEqual(
            confirm_call[2]["data"]["tax_id_collection[purchasing_as_business]"],
            "false",
        )
        for key in ("guid", "muid", "sid"):
            self.assertEqual(payment_method_call[2]["data"][key], confirm_call[2]["data"][key])
        self.assertFalse(any(url == link_gen.OAI_APPROVE for method, url, _ in scenario.calls))

    def test_requires_approval_keeps_multi_exit_pool_and_then_polls(self):
        scenario = KakaoHttpScenario(confirm_requires_approval=True)
        result, _ = self.run_flow(scenario)

        self.assertTrue(result["ok"])
        self.assertIn("approved", result["approve_states"])
        approve_calls = [call for call in scenario.calls if call[1] == link_gen.OAI_APPROVE]
        self.assertGreaterEqual(len(approve_calls), 1)
        self.assertTrue(all("region-kr" in call[2]["proxies"]["https"] for call in approve_calls))
        self.assertFalse(any("region-vn" in call[2]["proxies"]["https"] for call in approve_calls))
        self.assertEqual(result["link"], "https://web.nicepay.co.kr/kakao/checkout")

    def test_blocked_kakao_approval_uses_checkout_route_and_skips_empty_poll(self):
        class BlockedScenario(KakaoHttpScenario):
            def post(self, url, kwargs):
                if url == link_gen.OAI_APPROVE:
                    self.calls.append(("POST", url, kwargs))
                    return FakeResponse(payload={"result": "blocked"})
                return super().post(url, kwargs)

        scenario = BlockedScenario(confirm_requires_approval=True)
        result, _ = self.run_flow(scenario)

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_reason"], "approve_blocked")
        self.assertEqual(result["approve_states"], ["blocked"])
        approve_calls = [call for call in scenario.calls if call[1] == link_gen.OAI_APPROVE]
        checkout_call = next(call for call in scenario.calls if call[1] == link_gen.OAI_CHECKOUT)
        self.assertEqual(len(approve_calls), 1)
        self.assertEqual(
            approve_calls[0][2]["proxies"]["https"],
            checkout_call[2]["proxies"]["https"],
        )
        self.assertFalse(any(
            method == "GET" and url == f"{link_gen.STRIPE_API}/payment_pages/cs_test"
            for method, url, _ in scenario.calls
        ))

    def test_kakao_approve_explicit_401_is_not_retryable(self):
        class UnauthorizedScenario(KakaoHttpScenario):
            def post(self, url, kwargs):
                if url == link_gen.OAI_APPROVE:
                    self.calls.append(("POST", url, kwargs))
                    return FakeResponse(status_code=401, payload={})
                return super().post(url, kwargs)

        scenario = UnauthorizedScenario(confirm_requires_approval=True)
        result, _ = self.run_flow(scenario)

        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["retry_reason"], "approve_401")
        self.assertEqual(result["approve_http_status"], 401)
        self.assertEqual(result["approve_states"], ["401"])

    def test_transient_stripe_poll_timeout_retries_within_total_window(self):
        scenario = KakaoHttpScenario(confirm_requires_approval=True, poll_timeouts=1)
        events = []

        def log(phase, detail, **_kwargs):
            events.append((phase, detail))

        with mock.patch.object(link_gen.time, "sleep", return_value=None):
            result, _ = self.run_flow(scenario, log=log)

        self.assertTrue(result["ok"])
        self.assertEqual(result["link"], "https://web.nicepay.co.kr/kakao/checkout")
        self.assertIn(("poll", "轮询 Stripe redirect（最长 60 秒）"), events)
        poll_calls = [
            call for call in scenario.calls
            if call[0] == "GET" and call[1] == f"{link_gen.STRIPE_API}/payment_pages/cs_test"
        ]
        self.assertEqual(len(poll_calls), 2)

    def test_nonzero_kakao_checkout_stops_before_taxes_and_confirm(self):
        scenario = KakaoHttpScenario(post_promo_amount=12000)
        with self.assertRaisesRegex(RuntimeError, "checkout_not_kakao_trial.*amount=12000"):
            self.run_flow(scenario)

        urls = [url for _, url, _ in scenario.calls]
        self.assertNotIn(link_gen.OAI_TAXES, urls)
        self.assertFalse(any(url.endswith("/pre_confirm") for url in urls))
        self.assertFalse(any(url.endswith("/confirm") for url in urls))

    def test_proxy_pool_roles_force_kr_checkout_and_vn_promotion(self):
        checkout, promotion = link_gen._kakao_proxy_chain(self.checkout_seed, self.promotion_seed)
        self.assertIn("region-kr", checkout)
        self.assertIn("region-vn", promotion)
        self.assertIn("checkout.example", checkout)
        self.assertIn("promotion.example", promotion)

    def test_proxy_pool_roles_preserve_jp_promotion(self):
        jp_seed = "http://user-region-jp-sid-abc12345:pass@promotion.example:8000"
        checkout, promotion = link_gen._kakao_proxy_chain(self.checkout_seed, jp_seed)
        self.assertIn("region-kr", checkout)
        self.assertIn("region-jp", promotion)
        self.assertIn("promotion.example", promotion)

    def test_full_kakao_flow_preflights_jp_promotion_without_rewriting_it(self):
        scenario = KakaoHttpScenario()
        jp_seed = "http://user-region-jp-sid-abc12345:pass@promotion.example:8000"
        result, preflight = self.run_flow(scenario, update_proxy=jp_seed)

        self.assertTrue(result["ok"])
        self.assertEqual(
            [(country, label) for _, country, label in preflight],
            [("KR", "KR checkout/provider"), ("JP", "JP promotion")],
        )
        update_call = next(call for call in scenario.calls if call[1] == link_gen.OAI_UPDATE)
        self.assertIn("region-jp", update_call[2]["proxies"]["https"])


class KakaoProxyPreflightTests(unittest.TestCase):
    class IpSession:
        def __init__(self, country):
            self.country = country

        def get(self, *_args, **_kwargs):
            return FakeResponse(payload={"country": self.country})

        def close(self):
            pass

    class IpCffi:
        def __init__(self, country):
            self.country = country

        def Session(self, impersonate="chrome"):
            return KakaoProxyPreflightTests.IpSession(self.country)

    def test_preflight_accepts_expected_country_and_rejects_wrong_country(self):
        link_gen._preflight_kakao_proxy(self.IpCffi("KR"), "http://proxy:8000", "KR", "checkout")
        with self.assertRaisesRegex(RuntimeError, "出口国家 US，要求 KR"):
            link_gen._preflight_kakao_proxy(self.IpCffi("US"), "http://proxy:8000", "KR", "checkout")

    def test_http_fallback_reads_country_code_only_from_success_payload(self):
        self.assertEqual(link_gen._ip_country("ip_api_http", {"status": "success", "countryCode": "KR"}), "KR")
        self.assertEqual(link_gen._ip_country("ip_api_http", {"status": "fail", "countryCode": "KR"}), "")


class KakaoRedirectSafetyTests(unittest.TestCase):
    class RedirectSession:
        def __init__(self, location):
            self.location = location
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            return FakeResponse(status_code=302, headers={"Location": self.location})

    def test_provider_host_requires_https_and_real_domain_suffix(self):
        self.assertTrue(link_gen._is_kakao_provider_url("https://web.nicepay.co.kr/kakao/checkout"))
        self.assertTrue(link_gen._is_kakao_provider_url("https://online-pay.kakao.com/start"))
        self.assertFalse(link_gen._is_kakao_provider_url("http://web.nicepay.co.kr/kakao/checkout"))
        self.assertFalse(link_gen._is_kakao_provider_url("https://nicepay.com/checkout"))
        self.assertFalse(link_gen._is_kakao_provider_url("https://nicepay.evil.example/checkout"))
        self.assertFalse(link_gen._is_kakao_provider_url("https://kakao.attacker.test/start"))

    def test_redirect_never_requests_chatgpt_success_callback(self):
        callback = "https://chatgpt.com/backend-api/payments/checkout/openai_llc/cs_test/success"
        session = self.RedirectSession(callback)

        with self.assertRaisesRegex(RuntimeError, "不会触发支付成功回调"):
            link_gen._follow_kakao_redirect(
                session,
                "https://hooks.stripe.com/kakao/start",
                "http://proxy:8000",
            )

        self.assertEqual(session.urls, ["https://hooks.stripe.com/kakao/start"])


class KakaoTaskPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()
        db.save_registered({"email": "kakao@example.com", "access_token": "web-at"})
        db.save_proxy_pools({
            "kakao_pool1": "http://user-region-kr:pass@checkout.example:8000",
            "kakao_pool2": "http://user-region-vn:pass@promotion.example:8000",
        })

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        with account_ops._lock:
            account_ops._tasks.clear()
        # db.py 的短连接依赖对象析构关闭；Windows 删除临时 SQLite 前先回收游标/连接。
        gc.collect()
        self.tempdir.cleanup()

    def test_background_task_persists_final_provider_link(self):
        final_link = "https://web.nicepay.co.kr/kakao/checkout"
        with mock.patch.object(
            account_ops.link_gen,
            "generate_link",
            return_value={"ok": True, "link": final_link, "amount": "0", "approve_states": []},
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if (
                    task
                    and task["state"] in {"done", "partial", "failed"}
                    and task["finished_at"]
                ):
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao background task did not finish")

        self.assertEqual(task["state"], "done")
        row = db.list_registered(limit=10)[0]
        self.assertEqual(row["links"]["kakao"]["link"], final_link)

    def test_background_kakao_restarts_full_checkout_on_single_route_block(self):
        checkout_lines = [
            f"http://user-region-kr-{index}:pass@checkout{index}.example:8000"
            for index in range(1, 4)
        ]
        db.save_proxy_pools({"kakao_pool1": "\n".join(checkout_lines)})
        blocked = {
            "ok": False,
            "link": "",
            "amount": "0",
            "approve_states": ["blocked"],
            "retryable": True,
            "retry_reason": "approve_blocked",
        }
        final_link = "https://web.nicepay.co.kr/kakao/checkout"
        success = {
            "ok": True,
            "link": final_link,
            "amount": "0",
            "approve_states": ["approved"],
            "retryable": False,
            "retry_reason": "",
        }

        with (
            mock.patch.object(account_ops.random, "sample", return_value=checkout_lines),
            mock.patch.object(
                account_ops.link_gen,
                "generate_link",
                side_effect=[blocked, blocked, success],
            ) as generate,
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao retry task did not finish")

        self.assertEqual(task["state"], "done")
        self.assertEqual(generate.call_count, 3)
        used_proxies = [call.kwargs["checkout_proxy"] for call in generate.call_args_list]
        self.assertEqual(used_proxies, checkout_lines)
        for call in generate.call_args_list:
            self.assertEqual(call.kwargs["checkout_pool"], [call.kwargs["checkout_proxy"]])
            self.assertEqual(call.kwargs["approve_workers"], 1)
        row = db.get_registered("kakao@example.com")
        self.assertEqual(row["extra"]["links"]["kakao"]["link"], final_link)

    def test_background_kakao_retries_transient_network_error(self):
        checkout_lines = [
            "http://user-region-kr-1:pass@checkout1.example:8000",
            "http://user-region-kr-2:pass@checkout2.example:8000",
        ]
        db.save_proxy_pools({"kakao_pool1": "\n".join(checkout_lines)})
        final_link = "https://web.nicepay.co.kr/kakao/checkout"
        success = {
            "ok": True,
            "link": final_link,
            "amount": "0",
            "approve_states": ["approved"],
        }
        with (
            mock.patch.object(account_ops.random, "sample", return_value=checkout_lines),
            mock.patch.object(
                account_ops.link_gen,
                "generate_link",
                side_effect=[TimeoutError("curl: (28) Operation timed out"), success],
            ) as generate,
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao transient retry task did not finish")

        self.assertEqual(task["state"], "done")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(task["succeeded"], 1)
        self.assertTrue(any(
            event.get("code") == "KAKAO_RETRY_NETWORK"
            for event in task["events"]
        ))

    def test_kakao_sid_template_gets_three_unique_promotion_proxies(self):
        checkout_proxy = "http://user-region-kr-fixed:pass@checkout.example:8000"
        promotion_template = "http://user-region-jp-sid-{sid}-t-120:pass@promotion.example:8000"
        db.save_proxy_pools({
            "kakao_pool1": checkout_proxy,
            "kakao_pool2": promotion_template,
        })
        blocked = {
            "ok": False,
            "link": "",
            "amount": "0",
            "approve_states": ["blocked"],
            "retryable": True,
            "retry_reason": "approve_blocked",
        }
        success = {
            "ok": True,
            "link": "https://web.nicepay.co.kr/kakao/checkout",
            "amount": "0",
            "approve_states": ["approved"],
        }

        with (
            mock.patch.object(account_ops, "_new_kakao_proxy_sid", side_effect=[
                "a1b2c3d4", "e5f6a7b8", "c9d0e1f2",
            ]),
            mock.patch.object(
                account_ops.link_gen,
                "generate_link",
                side_effect=[blocked, blocked, success],
            ) as generate,
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao SID template retry task did not finish")

        self.assertEqual(task["state"], "done")
        self.assertEqual(generate.call_count, 3)
        promotion_proxies = [call.kwargs["update_proxy"] for call in generate.call_args_list]
        self.assertEqual(len(set(promotion_proxies)), 3)
        self.assertTrue(all("{sid}" not in proxy for proxy in promotion_proxies))
        self.assertEqual(
            [re.search(r"sid-([a-z0-9]{8})-t-120", proxy).group(1) for proxy in promotion_proxies],
            ["a1b2c3d4", "e5f6a7b8", "c9d0e1f2"],
        )

    def test_kakao_kr_and_promotion_sid_templates_use_distinct_sids_per_attempt(self):
        db.save_proxy_pools({
            "kakao_pool1": "http://user-region-kr-sid-{sid}-t-120:pass@checkout.example:8000",
            "kakao_pool2": "http://user-region-vn-sid-{sid}-t-120:pass@promotion.example:8000",
        })
        blocked = {
            "ok": False,
            "link": "",
            "amount": "0",
            "approve_states": ["blocked"],
            "retryable": True,
            "retry_reason": "approve_blocked",
        }
        success = {
            "ok": True,
            "link": "https://pay.nicepay.co.kr/kakao/checkout",
            "amount": "0",
            "approve_states": ["approved"],
        }
        with (
            mock.patch.object(account_ops, "_new_kakao_proxy_sid", side_effect=[
                "a1b2c3d4", "b1c2d3e4", "e5f6a7b8", "f5e6d7c8", "c9d0e1f2", "d9c0b1a2",
            ]),
            mock.patch.object(
                account_ops.link_gen,
                "generate_link",
                side_effect=[blocked, blocked, success],
            ) as generate,
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao KR SID template task did not finish")

        self.assertEqual(task["state"], "done")
        self.assertEqual(generate.call_count, 3)
        sid_pairs = []
        for call in generate.call_args_list:
            checkout_sid = re.search(r"sid-([a-z0-9]{8})-t-120", call.kwargs["checkout_proxy"]).group(1)
            promotion_sid = re.search(r"sid-([a-z0-9]{8})-t-120", call.kwargs["update_proxy"]).group(1)
            self.assertNotEqual(checkout_sid, promotion_sid)
            sid_pairs.append((checkout_sid, promotion_sid))
        self.assertEqual(
            sid_pairs,
            [
                ("a1b2c3d4", "b1c2d3e4"),
                ("e5f6a7b8", "f5e6d7c8"),
                ("c9d0e1f2", "d9c0b1a2"),
            ],
        )

    def test_kakao_nonretryable_result_does_not_rebuild_checkout(self):
        db.save_proxy_pools({
            "kakao_pool1": "http://user-region-kr:pass@checkout.example:8000",
            "kakao_pool2": "http://user-region-vn-sid-{sid}:pass@promotion.example:8000",
        })
        rejected = {
            "ok": False,
            "link": "",
            "amount": "0",
            "approve_states": ["401"],
            "approve_http_status": 401,
            "retryable": False,
            "retry_reason": "approve_401",
        }
        with mock.patch.object(
            account_ops.link_gen,
            "generate_link",
            return_value=rejected,
        ) as generate:
            task_id = account_ops.start_link_gen(["kakao@example.com"], "kakao")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Kakao non-retryable task did not finish")

        self.assertEqual(task["state"], "failed")
        self.assertEqual(generate.call_count, 1)
        self.assertFalse(any(
            event.get("code") == "KAKAO_RETRY_NEW_CHECKOUT"
            for event in task["events"]
        ))

    def test_upi_no_link_keeps_proxy_guidance_when_retryable_flag_is_false(self):
        db.save_proxy_pools({
            "upi_pool1": "http://user-region-in:pass@checkout.example:8000",
            "upi_pool2": "http://user-region-br:pass@promotion.example:8000",
        })
        blocked = {
            "ok": False,
            "link": "",
            "amount": "0",
            "approve_states": ["blocked"],
            "retryable": False,
            "retry_reason": "",
        }
        with mock.patch.object(
            account_ops.link_gen,
            "generate_link",
            return_value=blocked,
        ):
            task_id = account_ops.start_link_gen(["kakao@example.com"], "upi")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("UPI no-link task did not finish")

        self.assertEqual(task["state"], "failed")
        self.assertIn("住宅代理", task["errors"][0]["action"])
        self.assertNotIn("网页 AT", task["errors"][0]["action"])

    def test_kakao_runs_five_accounts_in_parallel(self):
        emails = [f"parallel-{index}@example.com" for index in range(5)]
        for index, email in enumerate(emails):
            db.save_registered({"email": email, "access_token": f"web-at-{index}"})
        db.save_proxy_pools({
            "kakao_pool1": "http://user-region-kr:pass@checkout.example:8000",
            "kakao_pool2": "http://user-region-vn-sid-{sid}:pass@promotion.example:8000",
        })

        barrier = threading.Barrier(5)
        counter_lock = threading.Lock()
        active = 0
        max_active = 0

        def generate(*_args, **_kwargs):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait(timeout=2)
                return {
                    "ok": True,
                    "link": "https://pay.nicepay.co.kr/kakao/checkout",
                    "amount": "0",
                    "approve_states": ["approved"],
                }
            finally:
                with counter_lock:
                    active -= 1

        with mock.patch.object(account_ops.link_gen, "generate_link", side_effect=generate) as mocked:
            task_id = account_ops.start_link_gen(emails, "kakao")
            deadline = time.time() + 5
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["state"] in {"done", "partial", "failed"} and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("Parallel Kakao task did not finish")

        self.assertEqual(task["state"], "done")
        self.assertEqual(task["completed"], 5)
        self.assertEqual(task["succeeded"], 5)
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(max_active, 5)

    def test_link_and_plus_updates_lock_the_full_json_merge(self):
        original_conn = db._conn

        class TrackingLock:
            held = False

            def __enter__(self):
                self.held = True
                return self

            def __exit__(self, *_args):
                self.held = False

        lock = TrackingLock()

        class ConnectionProxy:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, params=()):
                if "SELECT extra_json FROM registered" in sql:
                    self_case.assertTrue(lock.held, "extra_json merge read must hold db._lock")
                return self.inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        self_case = self
        with (
            mock.patch.object(db, "_lock", lock),
            mock.patch.object(db, "_conn", side_effect=lambda: ConnectionProxy(original_conn())),
        ):
            db.update_registered_link(
                "kakao@example.com", "kakao", "https://web.nicepay.co.kr/kakao/checkout",
            )
            db.update_plus_check(
                "kakao@example.com", {"status": "free", "label": "Free"},
            )

        saved = db.get_registered("kakao@example.com")["extra"]
        self.assertEqual(saved["links"]["kakao"]["link"], "https://web.nicepay.co.kr/kakao/checkout")
        self.assertEqual(saved["plus_check"]["status"], "free")


if __name__ == "__main__":
    unittest.main()
