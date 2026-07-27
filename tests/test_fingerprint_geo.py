import random
import unittest
from pathlib import Path
from unittest import mock

import fingerprint as fingerprint_module
import sentinel
import sentinel_quickjs
from auth_flow import AuthFlow, AuthResult


class FamilyRandom(random.Random):
    def __init__(self, family: str, seed: int = 7):
        super().__init__(seed)
        self.family = family

    def choices(self, population, weights=None, *, cum_weights=None, k=1):
        if list(population) == list(fingerprint_module._BROWSER_TYPES):
            return [self.family] * k
        return super().choices(
            population, weights=weights, cum_weights=cum_weights, k=k,
        )


class FingerprintConsistencyTests(unittest.TestCase):
    def make_fp(self, family: str) -> dict:
        return fingerprint_module.generate_fingerprint(FamilyRandom(family))

    def test_hardware_profile_matches_each_browser_family(self):
        expected = {
            "mac_safari": ("MacIntel", "Apple Computer, Inc.", None, 0),
            "ios_safari": ("iPhone", "Apple Computer, Inc.", None, 5),
            "chrome": ("Win32", "Google Inc.", "defined", 0),
            "firefox": ("Win32", "", None, 0),
        }
        for family, (platform, vendor, memory, touches) in expected.items():
            with self.subTest(family=family):
                fp = self.make_fp(family)
                self.assertEqual(fp["navigator_platform"], platform)
                self.assertEqual(fp["navigator_vendor"], vendor)
                self.assertEqual(fp["max_touch_points"], touches)
                self.assertGreater(fp["hardware_concurrency"], 0)
                if memory == "defined":
                    self.assertIn(fp["device_memory"], (4, 8))
                    self.assertTrue(fp["sec_ch_ua_full_version_list"])
                    self.assertEqual(fp["sec_ch_ua_model"], '""')
                else:
                    self.assertIsNone(fp["device_memory"])
                    self.assertEqual(fp["sec_ch_ua_full_version_list"], "")
                    self.assertIsNone(fp["sec_ch_ua_model"])

    def test_geo_update_never_changes_browser_tls_or_hardware(self):
        fp = self.make_fp("chrome")
        protected = {
            key: fp[key]
            for key in (
                "browser_type", "impersonate", "fallback_impersonates",
                "user_agent", "screen", "navigator_platform",
                "navigator_vendor", "hardware_concurrency", "device_memory",
                "max_touch_points", "device_pixel_ratio", "sec_ch_ua",
                "sec_ch_ua_full_version_list",
            )
        }
        fingerprint_module.apply_geo_profile(fp, "JP", random.Random(13))
        self.assertEqual(fp["timezone"], "Asia/Tokyo")
        self.assertEqual(fp["lang"], "ja-JP")
        self.assertTrue(fp["lang_full"].startswith("ja-JP"))
        self.assertEqual({key: fp[key] for key in protected}, protected)

    def test_unknown_country_preserves_existing_geo(self):
        fp = self.make_fp("firefox")
        before = (fp["timezone"], fp["lang"], fp["lang_full"])
        fingerprint_module.apply_geo_profile(fp, "ZZ", random.Random(1))
        self.assertEqual((fp["timezone"], fp["lang"], fp["lang_full"]), before)

    def test_tls_rotation_updates_chrome_version_but_keeps_geo(self):
        fp = self.make_fp("chrome")
        fingerprint_module.apply_geo_profile(fp, "DE", random.Random(2))
        geo = (fp["timezone"], fp["lang_full"])
        ua = fingerprint_module.ua_for_impersonate("chrome142", fp["user_agent"])
        fingerprint_module.sync_fingerprint_for_impersonate(fp, "chrome142", ua)
        self.assertIn("Chrome/142.0.0.0", fp["user_agent"])
        self.assertIn('v="142.0.0.0"', fp["sec_ch_ua_full_version_list"])
        self.assertEqual((fp["timezone"], fp["lang_full"]), geo)


class _Response:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Cookies:
    def get(self, _name, default=""):
        return default


class _TraceSession:
    def __init__(self):
        self.cookies = _Cookies()
        self.posts = []

    def get(self, url, **_kwargs):
        if "cdn-cgi/trace" in url:
            return _Response(200, "ip=203.0.113.1\nloc=JP\n")
        return _Response(200, "{}")

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response(200, payload={"token": "challenge"})


class AuthFlowGeoTests(unittest.TestCase):
    def make_flow(self, family="chrome"):
        fp = fingerprint_module.generate_fingerprint(FamilyRandom(family))
        flow = AuthFlow.__new__(AuthFlow)
        flow._fingerprint = fp
        flow._ua = fp["user_agent"]
        flow._country_code = ""
        flow._geo_seed = 123
        flow.session = _TraceSession()
        flow.result = AuthResult()
        return flow

    def test_check_proxy_only_changes_geo_fields(self):
        flow = self.make_flow("chrome")
        before = (
            id(flow.session), flow._fingerprint["browser_type"],
            flow._fingerprint["impersonate"], flow._ua,
            flow._fingerprint["hardware_concurrency"],
        )
        self.assertTrue(flow.check_proxy())
        self.assertEqual(flow._country_code, "JP")
        self.assertEqual(flow._fingerprint["timezone"], "Asia/Tokyo")
        self.assertEqual(
            (
                id(flow.session), flow._fingerprint["browser_type"],
                flow._fingerprint["impersonate"], flow._ua,
                flow._fingerprint["hardware_concurrency"],
            ),
            before,
        )

    def test_common_headers_emit_full_hints_only_for_chromium(self):
        chrome = self.make_flow("chrome")._common_headers()
        self.assertIn("sec-ch-ua-full-version-list", chrome)
        self.assertEqual(chrome["sec-ch-ua-model"], '""')
        firefox = self.make_flow("firefox")._common_headers()
        self.assertNotIn("sec-ch-ua", firefox)
        self.assertNotIn("sec-ch-ua-model", firefox)

    def test_auth_flow_passes_one_profile_to_sentinel(self):
        flow = self.make_flow("chrome")
        fingerprint_module.apply_geo_profile(
            flow._fingerprint, "JP", random.Random(5),
        )
        with mock.patch("sentinel.get_sentinel_token", return_value="{}") as call:
            flow.get_sentinel_token("device-id")
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["timezone"], "Asia/Tokyo")
        self.assertEqual(kwargs["navigator_platform"], "Win32")
        self.assertEqual(kwargs["navigator_vendor"], "Google Inc.")
        self.assertEqual(kwargs["user_agent"], flow._ua)


class SentinelProfileTests(unittest.TestCase):
    def test_python_timezone_fallback_covers_every_geo_profile(self):
        from sentinel import _FALLBACK_TZ_HOURS

        profile_timezones = {
            timezone_name
            for profile in fingerprint_module._COUNTRY_PROFILES.values()
            for timezone_name, _weight in profile["timezones"]
        }

        self.assertEqual(profile_timezones - set(_FALLBACK_TZ_HOURS), set())

    def test_python_config_uses_timezone_and_hardware_concurrency(self):
        generator = sentinel.SentinelTokenGenerator(
            user_agent="Mozilla/5.0 Chrome/142.0.0.0",
            navigator_platform="Win32",
            navigator_vendor="Google Inc.",
            hardware_concurrency=12,
            device_memory=8,
            timezone_name="Asia/Tokyo",
        )
        config = generator._get_config()
        self.assertIn("GMT+0900", config[1])
        self.assertEqual(config[17], 12)

    def test_challenge_sends_full_client_hints(self):
        session = _TraceSession()
        sentinel.fetch_sentinel_challenge(
            session,
            "device-id",
            user_agent="Chrome UA",
            sec_ch_ua='"Chromium";v="142"',
            sec_ch_ua_platform='"Windows"',
            sec_ch_ua_mobile="?0",
            sec_ch_ua_full_version_list='"Chromium";v="142.0.0.0"',
            sec_ch_ua_arch='"x86"',
            sec_ch_ua_bitness='"64"',
            sec_ch_ua_model='""',
            sec_ch_ua_platform_version='"15.0.0"',
        )
        headers = session.posts[0][1]["headers"]
        self.assertEqual(headers["sec-ch-ua-model"], '""')
        self.assertEqual(headers["sec-ch-ua-arch"], '"x86"')
        self.assertIn("142.0.0.0", headers["sec-ch-ua-full-version-list"])

    def test_quickjs_payload_preserves_empty_firefox_vendor(self):
        actions = []

        def fake_action(*, action, payload, **_kwargs):
            actions.append((action, dict(payload)))
            if action == "requirements":
                return {"request_p": "requirements"}
            return {"final_p": "proof", "t": "turnstile"}

        with (
            mock.patch.object(sentinel_quickjs, "_ensure_sdk_file", return_value=Path("sdk.js")),
            mock.patch.object(sentinel_quickjs, "_run_quickjs_action", side_effect=fake_action),
            mock.patch.object(
                sentinel_quickjs,
                "_fetch_sentinel_challenge",
                return_value={"token": "challenge"},
            ) as fetch,
        ):
            token = sentinel_quickjs.get_sentinel_token_via_quickjs(
                object(),
                "device-id",
                user_agent="Mozilla/5.0 Firefox/144.0",
                browser_type="firefox",
                platform="Win32",
                vendor="",
                hardware_concurrency=8,
                device_memory=None,
                timezone="Europe/Berlin",
                lang="de-DE",
                lang_full="de-DE,de;q=0.9",
            )
        self.assertTrue(token)
        payload = actions[0][1]
        self.assertEqual(payload["vendor"], "")
        self.assertNotIn("device_memory", payload)
        self.assertEqual(payload["timezone"], "Europe/Berlin")
        self.assertEqual(payload["hardware_concurrency"], 8)
        self.assertEqual(fetch.call_args.kwargs["accept_language"], "de-DE,de;q=0.9")


if __name__ == "__main__":
    unittest.main()
