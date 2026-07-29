import gc
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from webui import account_ops, db, proxy_config


class ProxyConfigUnitTests(unittest.TestCase):
    def test_sid_matches_official_eight_digit_format(self):
        values = {proxy_config.new_sid() for _ in range(20)}
        self.assertEqual(len(values), 20)
        self.assertTrue(all(len(value) == 8 and value.isdigit() for value in values))

    def test_1024_sid_is_eight_character_alphanumeric(self):
        template = (
            "http://user-region-KR-sid-{sid}-t-5:pass@us.1024proxy.io:3000"
        )
        values = {proxy_config.new_sid_for_proxy(template) for _ in range(20)}
        self.assertEqual(len(values), 20)
        self.assertTrue(all(len(value) == 8 and value.isalnum() for value in values))
        # 1024Proxy 不能使用纯数字 SID；生成器需保证测试集合中有字母。
        self.assertTrue(all(any(char.isalpha() for char in value) for value in values))

    def test_711_sid_remains_eight_digits(self):
        template = "http://user-region-KR-session-{sid}:pass@global.rotgb.711proxy.com:10000"
        values = [proxy_config.new_sid_for_proxy(template) for _ in range(20)]
        self.assertTrue(all(len(value) == 8 and value.isdigit() for value in values))

    def test_materialize_1024_proxy_uses_provider_sid_format(self):
        template = (
            "http://user-region-{country}-sid-{sid}-t-5:pass@us.1024proxy.io:3000"
        )
        actual = proxy_config.materialize_proxy(template, country="KR")
        self.assertIn("region-KR", actual)
        self.assertNotIn("{sid}", actual)
        sid = actual.split("-sid-", 1)[1].split("-t-", 1)[0]
        self.assertEqual(len(sid), 8)
        self.assertTrue(sid.isalnum())
        self.assertTrue(any(char.isalpha() for char in sid))

    def test_country_options_cover_payment_flows(self):
        self.assertTrue({"IN", "BR", "KR", "JP", "VN"}.issubset(proxy_config.SUPPORTED_COUNTRIES))
        self.assertEqual(proxy_config.POOL_COUNTRY_DEFAULTS["upi_pool1"], "IN")
        self.assertEqual(proxy_config.POOL_COUNTRY_DEFAULTS["upi_pool2"], "BR")
        self.assertEqual(proxy_config.POOL_COUNTRY_DEFAULTS["kakao_pool1"], "KR")
        self.assertEqual(proxy_config.POOL_COUNTRY_DEFAULTS["kakao_pool2"], "JP")

    def test_materialize_replaces_country_and_sid(self):
        template = "http://user-region-{country}-session-{sid}:pass@proxy.example:10000"
        actual = proxy_config.materialize_proxy(template, country="JP", sid="abc123")
        self.assertIn("region-JP", actual)
        self.assertIn("session-abc123", actual)
        self.assertNotIn("{country}", actual)
        self.assertNotIn("{sid}", actual)

    def test_bad_dynamic_node_is_replaced_before_task_starts(self):
        template = "http://user-region-JP-session-{sid}:pass@proxy.example:10000"
        with mock.patch.object(proxy_config, "_proxy_reachable", side_effect=[False, True]) as check:
            selected = proxy_config.pick_working_proxy(template, attempts=3)
        self.assertEqual(check.call_count, 2)
        first_proxy = check.call_args_list[0].args[0]
        second_proxy = check.call_args_list[1].args[0]
        self.assertNotEqual(first_proxy, second_proxy)
        self.assertEqual(selected, second_proxy)
        self.assertNotIn("{sid}", selected)

    def test_api_fallback_uses_selected_country_after_primary_failures(self):
        template = "http://user-region-JP-session-{sid}:pass@proxy.example:10000"
        with (
            mock.patch.object(proxy_config, "_proxy_reachable", side_effect=[False, False, True]),
            mock.patch.object(proxy_config, "fetch_api_proxy", return_value="http://198.51.100.10:10000") as fetch,
            mock.patch.object(proxy_config, "_proxy_country_matches", return_value=True),
        ):
            selected = proxy_config.pick_working_proxy(
                template,
                attempts=2,
                api_url="http://api.example/gen?zone=custom",
                country="JP",
            )
        self.assertEqual(selected, "http://198.51.100.10:10000")
        fetch.assert_called_once_with("http://api.example/gen?zone=custom", "JP", 10.0)

    def test_paylink_probe_rejects_partial_kr_connectivity_then_uses_api(self):
        template = "http://user-region-KR-session-{sid}:pass@proxy.example:10000"
        probes = (
            "https://chatgpt.com/api/auth/csrf",
            "https://api.stripe.com/v1/payment_pages",
            "https://web.nicepay.co.kr/",
        )
        with (
            mock.patch.object(proxy_config, "_proxy_urls_reachable", side_effect=[False, False, True]),
            mock.patch.object(proxy_config, "fetch_api_proxy", return_value="http://198.51.100.10:10000"),
            mock.patch.object(proxy_config, "_proxy_country_matches", return_value=True),
        ):
            selected = proxy_config.pick_working_proxy(
                template,
                attempts=2,
                api_url="http://api.example/gen",
                country="KR",
                probe_urls=probes,
                verify_country=True,
            )
        self.assertEqual(selected, "http://198.51.100.10:10000")

    def test_api_url_overrides_random_region_with_dropdown_country(self):
        url = proxy_config.build_api_proxy_url(
            "http://api.example/gen?zone=custom&sessType=rotating&stype=text",
            "BR",
        )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["region"], "BR")
        self.assertEqual(query["proto"], "http")
        self.assertEqual(query["stype"], "json")
        self.assertEqual(query["sessType"], "sticky")
        self.assertEqual(query["sessTime"], "5")

    def test_1024_api_url_uses_txt_and_selected_country(self):
        url = proxy_config.build_api_proxy_url(
            "https://white.1024proxy.com/white/api?region=JP&num=1&time=10&format=1&type=txt",
            "KR",
        )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["region"], "KR")
        self.assertEqual(query["num"], "1")
        self.assertEqual(query["format"], "1")
        self.assertEqual(query["type"], "txt")

    def test_fetch_1024_api_txt_response(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"105.33.13.23:8080\n"

        with mock.patch.object(proxy_config.urllib.request, "urlopen", return_value=Response()) as open_url:
            actual = proxy_config.fetch_api_proxy(
                "https://white.1024proxy.com/white/api?region=JP&num=1&time=10&format=1&type=txt",
                "JP",
            )
        self.assertEqual(actual, "http://105.33.13.23:8080")
        request = open_url.call_args.args[0]
        self.assertIn("region=JP", request.full_url)
        self.assertIn("type=txt", request.full_url)
        self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")

    def test_frontend_uses_country_selects_instead_of_proxy_textareas(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        for key in proxy_config.POOL_COUNTRY_DEFAULTS:
            self.assertIn(f'<select id="{key}">', html)
            self.assertNotIn(f'<textarea id="{key}"', html)
        self.assertIn('body[key + "_country"]', script)
        self.assertIn('<select id="regProxyCountry">', html)
        self.assertIn('<select id="autoProxyCountry"', html)
        self.assertNotIn('id="regProxy"', html)
        self.assertNotIn('id="autoProxyPool"', html)
        self.assertIn('proxy_country: $("#regProxyCountry").value', script)
        self.assertIn('proxy_country: $("#autoProxyCountry").value', script)


class ProxyCountryDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        gc.collect()
        self.tempdir.cleanup()

    def test_defaults_and_country_save_use_private_template(self):
        db.set_setting(
            "global_proxy_template",
            "http://user-region-{country}-session-{sid}:pass@proxy.example:10000",
        )
        config = db.get_proxy_country_config()
        self.assertEqual(config["selected"], proxy_config.POOL_COUNTRY_DEFAULTS)

        db.save_proxy_countries({"kakao_pool2_country": "BR"})
        pool = db.get_proxy_pools()["kakao_pool2"]
        self.assertIn("region-BR", pool)
        self.assertIn("{sid}", pool)
        self.assertEqual(db.get_proxy_country_config()["selected"]["kakao_pool2"], "BR")

    def test_invalid_country_is_rejected(self):
        db.set_setting("global_proxy_template", "http://user-region-{country}:pass@proxy.example:1")
        with self.assertRaisesRegex(ValueError, "不支持的代理国家"):
            db.save_proxy_countries({"upi_pool1_country": "ZZ"})

    def test_global_proxy_materializes_a_new_sid_per_task(self):
        db.set_setting(
            "global_proxy_template",
            "http://user-region-{country}-session-{sid}:pass@proxy.example:10000",
        )
        first = db.materialize_global_proxy("JP")
        second = db.materialize_global_proxy("JP")
        self.assertIn("region-JP", first)
        self.assertNotIn("{sid}", first)
        self.assertNotEqual(first, second)


class UpiDynamicSidTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()
        db.save_registered({"email": "upi@example.com", "access_token": "web-at"})
        db.save_proxy_pools({
            "upi_pool1": "http://user-region-IN-session-{sid}:pass@proxy.example:10000",
            "upi_pool2": "http://user-region-BR-session-{sid}:pass@proxy.example:10000",
        })

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        with account_ops._lock:
            account_ops._tasks.clear()
        gc.collect()
        self.tempdir.cleanup()

    def test_upi_materializes_checkout_promotion_and_ten_approvals(self):
        success = {"ok": True, "link": "https://payments.stripe.com/upi/instructions/demo", "amount": "0"}
        with (
            mock.patch.object(account_ops.link_gen, "generate_link", return_value=success) as generate,
            mock.patch.object(
                account_ops.proxy_config,
                "pick_working_proxy",
                side_effect=lambda proxy, **_kwargs: account_ops._materialize_proxy_template(
                    proxy,
                    account_ops._new_kakao_proxy_sid()
                    if account_ops.KAKAO_PROXY_SID_PLACEHOLDER in proxy else "",
                ),
            ),
        ):
            task_id = account_ops.start_link_gen(["upi@example.com"], "upi")
            deadline = time.time() + 2
            while time.time() < deadline:
                task = account_ops.get_task(task_id)
                if task and task["finished_at"]:
                    break
                time.sleep(0.01)
            else:
                self.fail("UPI task did not finish")

        call = generate.call_args
        proxies = call.kwargs["checkout_pool"]
        self.assertEqual(len(proxies), 10)
        self.assertEqual(len(set(proxies)), 10)
        self.assertNotIn("{sid}", call.kwargs["checkout_proxy"])
        self.assertNotIn("{sid}", call.kwargs["update_proxy"])
        self.assertTrue(all("{sid}" not in proxy for proxy in proxies))


if __name__ == "__main__":
    unittest.main()
