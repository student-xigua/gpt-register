import base64
import json
import unittest
import urllib.parse

from webui import manual_oauth


def jwt(payload: dict) -> str:
    def enc(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


class ManualOAuthTests(unittest.TestCase):
    def setUp(self):
        manual_oauth._reset_for_tests()

    def tearDown(self):
        manual_oauth._reset_for_tests()

    def test_start_flow_builds_pkce_url_without_exposing_verifier(self):
        result = manual_oauth.start_flow("Person@Example.com")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(result["auth_url"]).query))

        self.assertEqual(result["email"], "person@example.com")
        self.assertEqual(query["client_id"], manual_oauth.CLIENT_ID)
        self.assertEqual(query["redirect_uri"], manual_oauth.REDIRECT_URI)
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertIn("offline_access", query["scope"])
        self.assertNotIn("verifier", result)
        self.assertNotIn("code_verifier", result["auth_url"])

    def test_complete_flow_generates_standard_sub2_and_is_single_use(self):
        started = manual_oauth.start_flow("person@example.com")
        state = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(started["auth_url"]).query
        ))["state"]
        access = jwt({
            "https://api.openai.com/profile": {"email": "person@example.com"},
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
        })
        callback = f"http://localhost:1455/auth/callback?code=auth-code&state={state}"

        result = manual_oauth.complete_flow(
            started["flow_id"],
            callback,
            exchanger=lambda code, verifier: {
                "access_token": access,
                "refresh_token": "rt-new",
                "id_token": "id-new",
            },
        )
        payload = json.loads(result["content"])

        self.assertEqual(result["email"], "person@example.com")
        self.assertEqual(result["refresh_token"], "rt-new")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["proxies"], [])
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "person@example.com")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "acct-123")
        with self.assertRaisesRegex(manual_oauth.ManualOAuthError, "已过期"):
            manual_oauth.complete_flow(started["flow_id"], callback)

    def test_invalid_state_does_not_consume_flow(self):
        started = manual_oauth.start_flow("person@example.com")
        callback = "http://localhost:1455/auth/callback?code=x&state=wrong"
        with self.assertRaisesRegex(manual_oauth.ManualOAuthError, "state 不匹配"):
            manual_oauth.complete_flow(started["flow_id"], callback)

        state = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(started["auth_url"]).query
        ))["state"]
        good = f"http://localhost:1455/auth/callback?code=x&state={state}"
        result = manual_oauth.complete_flow(
            started["flow_id"], good,
            exchanger=lambda *_: {
                "access_token": jwt({"email": "person@example.com"}),
                "refresh_token": "rt",
            },
        )
        self.assertEqual(result["email"], "person@example.com")

    def test_authorized_email_must_match_selected_account(self):
        started = manual_oauth.start_flow("selected@example.com")
        state = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(started["auth_url"]).query
        ))["state"]
        callback = f"http://localhost:1455/auth/callback?code=x&state={state}"
        with self.assertRaisesRegex(manual_oauth.ManualOAuthError, "授权账号与所选账号不一致"):
            manual_oauth.complete_flow(
                started["flow_id"], callback,
                exchanger=lambda *_: {
                    "access_token": jwt({"email": "other@example.com"}),
                    "refresh_token": "rt",
                },
            )


if __name__ == "__main__":
    unittest.main()
