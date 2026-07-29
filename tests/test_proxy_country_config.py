import gc
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from webui import account_ops, db, proxy_config


class ProxyConfigUnitTests(unittest.TestCase):
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
        with mock.patch.object(account_ops.link_gen, "generate_link", return_value=success) as generate:
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
