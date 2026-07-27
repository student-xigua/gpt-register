from __future__ import annotations

import imaplib
import time
import unittest
import urllib.error
from unittest import mock

from mail_outlook import (
    FatalOutlookMailError,
    IMAP_SERVERS,
    OutlookMailProvider,
    _graph_list_messages,
    _request_access_token,
    fetch_otp_via_graph,
    fetch_otp_via_imap,
)


class GraphReliabilityTests(unittest.TestCase):
    def _provider(self) -> OutlookMailProvider:
        return OutlookMailProvider(
            email="source@outlook.com",
            password="",
            client_id="client-id",
            refresh_token="refresh-token",
        )

    def test_graph_network_error_falls_back_to_imap(self):
        with (
            mock.patch(
                "mail_outlook.fetch_otp_via_graph",
                side_effect=urllib.error.URLError("network unavailable"),
            ),
            mock.patch(
                "mail_outlook.fetch_otp_via_imap",
                return_value="123456",
            ) as imap,
        ):
            otp = self._provider().wait_for_otp(
                "target@outlook.com",
                timeout=90,
                issued_after=time.time(),
            )

        self.assertEqual(otp, "123456")
        self.assertEqual(imap.call_args.kwargs["target_email"], "target@outlook.com")

    def test_graph_request_timeout_tracks_remaining_deadline(self):
        captured: list[float] = []

        def forbidden(*_args, timeout, **_kwargs):
            captured.append(timeout)
            raise urllib.error.HTTPError(
                "https://graph.microsoft.com", 400, "Bad Request", {}, None,
            )

        with (
            mock.patch(
                "mail_outlook._request_access_token",
                return_value={"access_token": "token"},
            ),
            mock.patch("mail_outlook._graph_list_messages", side_effect=forbidden),
        ):
            with self.assertRaises(FatalOutlookMailError):
                fetch_otp_via_graph(
                    "source@outlook.com",
                    "refresh-token",
                    "client-id",
                    deadline=time.time() + 30,
                )
            with self.assertRaises(FatalOutlookMailError):
                fetch_otp_via_graph(
                    "source@outlook.com",
                    "refresh-token",
                    "client-id",
                    deadline=time.time() + 0.25,
                )

        self.assertEqual(captured[0], 8.0)
        self.assertEqual(captured[1], 1.0)

    def test_token_endpoints_share_the_supplied_deadline(self):
        class Clock:
            now = 3000.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        timeouts: list[float] = []

        def timeout_request(_request, *, timeout):
            timeouts.append(timeout)
            clock.now += timeout
            raise TimeoutError("token endpoint unavailable")

        with (
            mock.patch("mail_outlook.time.time", side_effect=clock),
            mock.patch(
                "mail_outlook.urllib.request.urlopen",
                side_effect=timeout_request,
            ),
        ):
            with self.assertRaisesRegex(TimeoutError, "token deadline"):
                _request_access_token(
                    "refresh-token",
                    "client-id",
                    "scope",
                    deadline=clock.now + 3,
                )

        self.assertEqual(len(timeouts), 3)
        self.assertAlmostEqual(sum(timeouts), 3.0)
        self.assertTrue(all(0 < value <= 1.0 for value in timeouts))

    def test_graph_initial_and_refresh_tokens_receive_deadline(self):
        deadline = time.time() + 5
        http_401 = urllib.error.HTTPError(
            "https://graph.microsoft.com", 401, "Unauthorized", {}, None,
        )
        http_400 = urllib.error.HTTPError(
            "https://graph.microsoft.com", 400, "Bad Request", {}, None,
        )
        with (
            mock.patch(
                "mail_outlook._request_access_token",
                side_effect=(
                    {"access_token": "initial", "refresh_token": "rotated"},
                    {"access_token": "refreshed"},
                ),
            ) as token_request,
            mock.patch(
                "mail_outlook._graph_list_messages",
                side_effect=(http_401, http_400),
            ),
        ):
            with self.assertRaisesRegex(FatalOutlookMailError, "HTTP 400"):
                fetch_otp_via_graph(
                    "source@outlook.com",
                    "refresh-token",
                    "client-id",
                    deadline=deadline,
                )

        self.assertEqual(token_request.call_count, 2)
        self.assertTrue(
            all(call.kwargs["deadline"] == deadline for call in token_request.call_args_list)
        )

    def test_graph_retry_keeps_400_401_403_fatal(self):
        def http_error(code: int) -> urllib.error.HTTPError:
            return urllib.error.HTTPError(
                "https://graph.microsoft.com", code, "error", {}, None,
            )

        for retry_status in (400, 401, 403):
            with self.subTest(retry_status=retry_status):
                with (
                    mock.patch(
                        "mail_outlook._request_access_token",
                        return_value={"access_token": "token"},
                    ),
                    mock.patch(
                        "mail_outlook._graph_list_messages",
                        side_effect=[http_error(401), http_error(retry_status)],
                    ),
                ):
                    with self.assertRaisesRegex(
                        FatalOutlookMailError,
                        f"HTTP {retry_status}",
                    ):
                        fetch_otp_via_graph(
                            "source@outlook.com",
                            "refresh-token",
                            "client-id",
                            deadline=time.time() + 5,
                        )

    def test_graph_list_messages_passes_explicit_timeout(self):
        response = mock.Mock()
        response.read.return_value = b'{"value": []}'
        with mock.patch(
            "mail_outlook.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            self.assertEqual(
                _graph_list_messages("access-token", "inbox", timeout=2.5),
                [],
            )

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2.5)

    def test_graph_and_imap_share_one_total_deadline(self):
        class Clock:
            now = 1000.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()

        def graph_timeout(*_args, **_kwargs):
            clock.now += 15.0
            raise TimeoutError("graph budget exhausted")

        with (
            mock.patch("mail_outlook.time.time", side_effect=clock),
            mock.patch(
                "mail_outlook.fetch_otp_via_graph",
                side_effect=graph_timeout,
            ) as graph,
            mock.patch(
                "mail_outlook.fetch_otp_via_imap",
                return_value="654321",
            ) as imap,
        ):
            otp = self._provider().wait_for_otp(
                "target@outlook.com",
                timeout=90,
                issued_after=clock.now,
            )

        self.assertEqual(otp, "654321")
        self.assertEqual(graph.call_args.kwargs["deadline"], 1015.0)
        self.assertEqual(imap.call_args.kwargs["deadline"], 1090.0)
        self.assertEqual(imap.call_args.kwargs["timeout"], 90)

    def test_ten_second_timeout_is_allowed_without_changing_default(self):
        class Clock:
            now = 2000.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        with (
            mock.patch("mail_outlook.time.time", side_effect=clock),
            mock.patch(
                "mail_outlook.fetch_otp_via_graph",
                side_effect=TimeoutError("graph unavailable"),
            ) as graph,
            mock.patch(
                "mail_outlook.fetch_otp_via_imap",
                return_value="112233",
            ) as imap,
        ):
            otp = self._provider().wait_for_otp(
                "target@outlook.com", timeout=10, issued_after=clock.now,
            )

        self.assertEqual(otp, "112233")
        self.assertAlmostEqual(graph.call_args.kwargs["deadline"], 2000.0 + 10 / 3)
        self.assertEqual(imap.call_args.kwargs["deadline"], 2010.0)
        self.assertEqual(imap.call_args.kwargs["timeout"], 10)


class ImapReliabilityTests(unittest.TestCase):
    def test_xoauth2_token_failure_falls_back_to_password(self):
        raw_message = (
            b"From: no-reply@openai.com\r\n"
            b"To: target@outlook.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Your verification code is 135790\r\n"
        )
        password_logins: list[tuple[str, str]] = []

        class FakeImap:
            def login(self, email: str, password: str):
                password_logins.append((email, password))
                return "OK", [b""]

            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

            def select(self, *_args, **_kwargs):
                return "OK", [b""]

            def search(self, *_args, **_kwargs):
                return "OK", [b"1"]

            def fetch(self, *_args, **_kwargs):
                return "OK", [(b"1", raw_message)]

            def logout(self):
                return "BYE", [b""]

        with (
            mock.patch(
                "mail_outlook._request_access_token",
                side_effect=FatalOutlookMailError("invalid_grant"),
            ) as token_request,
            mock.patch(
                "mail_outlook.imaplib.IMAP4_SSL",
                return_value=FakeImap(),
            ),
        ):
            otp = fetch_otp_via_imap(
                "source@outlook.com",
                "refresh-token",
                "client-id",
                password="mail-password",
                deadline=time.time() + 5,
                target_email="target@outlook.com",
            )

        self.assertEqual(otp, "135790")
        self.assertEqual(
            password_logins,
            [("source@outlook.com", "mail-password")],
        )
        self.assertIn("deadline", token_request.call_args.kwargs)

    def test_xoauth2_failure_on_first_host_continues_to_second(self):
        raw_message = (
            b"From: no-reply@openai.com\r\n"
            b"To: target@outlook.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Your verification code is 246810\r\n"
        )
        connected_hosts: list[str] = []

        class FakeImap:
            def __init__(self, host: str):
                self.host = host

            def authenticate(self, *_args):
                if self.host == IMAP_SERVERS[0]:
                    raise imaplib.IMAP4.error("authentication failed")
                return "OK", [b""]

            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

            def select(self, *_args, **_kwargs):
                return "OK", [b""]

            def search(self, *_args, **_kwargs):
                return "OK", [b"1"]

            def fetch(self, *_args, **_kwargs):
                return "OK", [(b"1", raw_message)]

            def logout(self):
                return "BYE", [b""]

        def connect(host, _port, *, timeout):
            self.assertGreater(timeout, 0)
            connected_hosts.append(host)
            return FakeImap(host)

        with (
            mock.patch(
                "mail_outlook._request_access_token",
                return_value={"access_token": "access-token"},
            ) as token_request,
            mock.patch("mail_outlook.imaplib.IMAP4_SSL", side_effect=connect),
        ):
            otp = fetch_otp_via_imap(
                "source@outlook.com",
                "refresh-token",
                "client-id",
                deadline=time.time() + 5,
                target_email="target@outlook.com",
            )

        self.assertEqual(otp, "246810")
        self.assertEqual(connected_hosts, IMAP_SERVERS)
        self.assertGreater(token_request.call_args.kwargs["deadline"], time.time())

    def test_fatal_token_failure_without_password_is_raised(self):
        with (
            mock.patch(
                "mail_outlook._request_access_token",
                side_effect=FatalOutlookMailError("invalid_grant"),
            ),
            mock.patch("mail_outlook.imaplib.IMAP4_SSL") as connect,
        ):
            with self.assertRaisesRegex(FatalOutlookMailError, "invalid_grant"):
                fetch_otp_via_imap(
                    "source@outlook.com",
                    "refresh-token",
                    "client-id",
                    deadline=time.time() + 5,
                )

        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
