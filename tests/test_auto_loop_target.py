from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path
from unittest import mock

from webui import auto_loop


class AutoLoopTargetTests(unittest.TestCase):
    @staticmethod
    def _request_model_fields() -> dict[str, ast.expr]:
        source = (Path(__file__).parents[1] / "webui" / "app.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(source)
        model = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AutoLoopStartReq"
        )
        return {
            node.target.id: node.value
            for node in model.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        }

    @staticmethod
    def _controller(target_count: int) -> auto_loop.AutoLoopController:
        controller = auto_loop.AutoLoopController()
        controller._state = auto_loop.AutoLoopState.RUNNING
        controller._target_count = target_count
        controller._options = {"cool_down_seconds": 0.01}
        return controller

    def test_request_defaults_keep_unlimited_target_and_existing_otp_timeout(self):
        fields = self._request_model_fields()
        target = fields["target_count"]

        self.assertIsInstance(target, ast.Call)
        self.assertEqual(ast.literal_eval(target.args[0]), 0)
        self.assertEqual(ast.literal_eval(fields["otp_timeout"]), 180)

    def test_invalid_target_is_rejected_in_api_and_controller(self):
        target = self._request_model_fields()["target_count"]
        bounds = {keyword.arg: ast.literal_eval(keyword.value) for keyword in target.keywords}
        self.assertEqual(bounds["ge"], 0)
        self.assertEqual(bounds["le"], 100000)

        for target_count in (-1, 100001):
            controller = auto_loop.AutoLoopController()
            result = controller.start({"target_count": target_count})
            self.assertFalse(result["ok"])
            self.assertEqual(controller._state, auto_loop.AutoLoopState.STOPPED)

    def test_frontend_rejects_target_capability_mismatch_and_busts_cache(self):
        root = Path(__file__).parents[1] / "webui" / "static"
        script = (root / "app.js").read_text(encoding="utf-8")
        page = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn('hasOwnProperty.call(capability, "target_count")', script)
        self.assertIn("Number(started.target_count) !== options.target_count", script)
        self.assertIn("/api/auto/stop?force=true", script)
        self.assertRegex(page, r'app\.js\?v=[^"\s]+')

    def test_target_slots_are_reserved_atomically(self):
        controller = self._controller(3)
        thread_count = 20
        barrier = threading.Barrier(thread_count + 1)
        results: list[bool] = []
        results_lock = threading.Lock()

        def reserve():
            barrier.wait()
            reserved = controller._reserve_slot()
            with results_lock:
                results.append(reserved)

        threads = [threading.Thread(target=reserve) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), thread_count)
        self.assertEqual(sum(results), 3)
        self.assertEqual(controller._in_flight, 3)
        self.assertLessEqual(
            controller._registered_ok + controller._in_flight,
            controller._target_count,
        )

    def test_failed_run_releases_capacity_and_only_success_reaches_target(self):
        controller = self._controller(1)
        controller._options["proxy"] = "http://user-region-JP-session-{sid}:pass@proxy.example:10000"
        wait_results = iter(((False, "network"), (True, "")))

        with (
            mock.patch.object(auto_loop.db, "get_setting", return_value="cf_temp"),
            mock.patch.object(auto_loop.db, "stats", return_value={}),
            mock.patch.object(
                auto_loop.registrar,
                "start_registration",
                side_effect=("run-failed", "run-success"),
            ) as start_registration,
            mock.patch.object(
                controller,
                "_wait_run_finish",
                side_effect=lambda _run_id: next(wait_results),
            ),
            mock.patch.object(
                auto_loop.proxy_config,
                "pick_working_proxy",
                side_effect=auto_loop.proxy_config.materialize_proxy,
            ),
        ):
            controller._worker_loop(0)

        self.assertEqual(start_registration.call_count, 2)
        proxies = [call.args[1]["proxy"] for call in start_registration.call_args_list]
        self.assertEqual(len(set(proxies)), 2)
        self.assertTrue(all("{sid}" not in proxy for proxy in proxies))
        self.assertEqual(controller._registered_ok, 1)
        self.assertEqual(controller._registered_fail, 1)
        self.assertEqual(controller._in_flight, 0)
        self.assertTrue(controller._stop_claim_event.is_set())
        self.assertFalse(controller._stop_event.is_set())

    def test_concurrent_workers_never_start_more_than_target(self):
        controller = self._controller(3)
        all_started = threading.Event()
        started_lock = threading.Lock()
        started_count = 0

        def wait_for_finish(_run_id: str):
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == controller._target_count:
                    all_started.set()
            self.assertTrue(all_started.wait(2))
            return True, ""

        run_ids = iter(f"run-{index}" for index in range(20))
        with (
            mock.patch.object(auto_loop.db, "get_setting", return_value="cf_temp"),
            mock.patch.object(auto_loop.db, "stats", return_value={}),
            mock.patch.object(
                auto_loop.registrar,
                "start_registration",
                side_effect=lambda *_args: next(run_ids),
            ) as start_registration,
            mock.patch.object(controller, "_wait_run_finish", side_effect=wait_for_finish),
        ):
            threads = [
                threading.Thread(target=controller._worker_loop, args=(worker_id,))
                for worker_id in range(10)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(start_registration.call_count, 3)
        self.assertEqual(controller._registered_ok, 3)
        self.assertEqual(controller._registered_fail, 0)
        self.assertEqual(controller._in_flight, 0)

    def test_manual_stop_waits_for_active_run_and_counts_terminal_result(self):
        controller = self._controller(1)
        wait_started = threading.Event()
        allow_finish = threading.Event()

        def wait_for_finish(_run_id: str):
            wait_started.set()
            self.assertTrue(allow_finish.wait(2))
            return True, ""

        with (
            mock.patch.object(auto_loop.db, "get_setting", return_value="cf_temp"),
            mock.patch.object(auto_loop.db, "stats", return_value={}),
            mock.patch.object(auto_loop.registrar, "start_registration", return_value="run-1"),
            mock.patch.object(controller, "_wait_run_finish", side_effect=wait_for_finish),
        ):
            worker = threading.Thread(target=controller._worker_loop, args=(0,))
            worker.start()
            self.assertTrue(wait_started.wait(2))
            self.assertTrue(controller.stop()["ok"])
            self.assertTrue(worker.is_alive())
            self.assertFalse(controller._stop_event.is_set())
            allow_finish.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(controller._registered_ok, 1)
        self.assertEqual(controller._registered_fail, 0)
        self.assertEqual(controller._in_flight, 0)

    def test_second_stop_forces_a_stuck_active_run(self):
        controller = self._controller(0)

        first = controller.stop()
        repeated = controller.stop()
        explicit = controller.stop(force=True)

        self.assertFalse(first["forced"])
        self.assertFalse(repeated["forced"])
        self.assertTrue(explicit["forced"])
        self.assertTrue(controller._stop_claim_event.is_set())
        self.assertTrue(controller._stop_event.is_set())
        self.assertIn("强制停止", controller._last_message)
        with mock.patch.object(auto_loop.db, "_conn") as connect:
            self.assertEqual(controller._wait_run_finish("stuck-run"), (False, ""))
        connect.assert_not_called()

    def test_wait_retries_query_errors_and_nonterminal_timeout(self):
        controller = self._controller(1)

        class Connection:
            def __init__(self, status):
                self.status = status

            def execute(self, *_args):
                return self

            def fetchone(self):
                return {"status": self.status, "error_category": ""}

            def close(self):
                pass

        with (
            mock.patch.object(
                auto_loop.db,
                "_conn",
                side_effect=(RuntimeError("db busy"), Connection("running"), Connection("done")),
            ),
            mock.patch.object(auto_loop.time, "sleep"),
        ):
            self.assertEqual(controller._wait_run_finish("run-1", timeout=0.001), (True, ""))

    def test_snapshot_failure_does_not_leak_started_slot(self):
        controller = self._controller(1)
        with (
            mock.patch.object(auto_loop.db, "get_setting", return_value="cf_temp"),
            mock.patch.object(auto_loop.db, "stats", side_effect=RuntimeError("stats busy")),
            mock.patch.object(auto_loop.registrar, "start_registration", return_value="run-1"),
            mock.patch.object(controller, "_wait_run_finish", return_value=(True, "")),
        ):
            controller._worker_loop(0)

        self.assertEqual(controller._registered_ok, 1)
        self.assertEqual(controller._in_flight, 0)

    def test_paused_controller_cannot_reserve_a_slot(self):
        controller = self._controller(2)
        self.assertTrue(controller.pause()["ok"])
        self.assertFalse(controller._reserve_slot())
        self.assertEqual(controller._in_flight, 0)

    def test_status_reports_target_remaining_and_in_flight(self):
        controller = self._controller(5)
        controller._registered_ok = 2
        self.assertTrue(controller._reserve_slot())

        with mock.patch.object(auto_loop.db, "stats", return_value={}):
            status = controller.status()

        self.assertEqual(status["target_count"], 5)
        self.assertEqual(status["remaining"], 3)
        self.assertEqual(status["in_flight"], 1)

    def test_zero_target_remains_unlimited(self):
        controller = self._controller(0)

        self.assertTrue(all(controller._reserve_slot() for _ in range(25)))
        with mock.patch.object(auto_loop.db, "stats", return_value={}):
            status = controller.status()

        self.assertEqual(status["target_count"], 0)
        self.assertIsNone(status["remaining"])
        self.assertEqual(status["in_flight"], 25)


if __name__ == "__main__":
    unittest.main()
