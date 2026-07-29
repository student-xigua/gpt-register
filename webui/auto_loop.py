"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from . import db, proxy_config, registrar

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      target_count:         目标成功数（0 表示不限量）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        # 只阻止领取新任务；与手动停止分离，确保达标时在途注册正常收尾并计数。
        self._stop_claim_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        self._target_count: int = 0
        # 已预约但尚未完成的任务数；预约覆盖 claim → start → run 完成整个区间。
        self._in_flight: int = 0

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            try:
                target_count = int((options or {}).get("target_count") or 0)
            except (TypeError, ValueError):
                return {"ok": False, "error": "target_count 必须是 0-100000 的整数"}
            if not 0 <= target_count <= 100000:
                return {"ok": False, "error": "target_count 必须在 0-100000 之间"}
            # 重置
            self._stop_event.clear()
            self._stop_claim_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._in_flight = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            self._target_count = target_count
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
            "remaining": self._target_count if self._target_count else None,
            "in_flight": self._in_flight,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self, *, force: bool = False) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_claim_event.set()
            self._pause_event.clear()
            if force:
                self._stop_event.set()
                self._last_message = (
                    "已请求强制停止（停止监控；已启动注册仍可能在后台完成）"
                )
            else:
                self._last_message = (
                    "已请求停止（当前 worker 跑完才生效；再次点击可强制停止）"
                )
        self._broadcast("state", self._snapshot())
        return {"ok": True, "forced": bool(force)}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "in_flight": self._in_flight,
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _proxy_for_worker(self, worker_id: int) -> str:
        """按 worker_id 从代理池里挑一个代理。空池时回退到 options.proxy。"""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    def _reserve_slot(self) -> bool:
        """原子预约一个目标槽位；成功后必须完成或调用 ``_release_slot``。"""
        with self._lock:
            if (
                self._state != AutoLoopState.RUNNING
                or self._pause_event.is_set()
                or self._stop_event.is_set()
                or self._stop_claim_event.is_set()
            ):
                return False
            if self._target_count and (
                self._registered_ok + self._in_flight >= self._target_count
            ):
                return False
            self._in_flight += 1
            return True

    def _release_slot(self) -> None:
        """释放尚未启动成功的预约槽位。"""
        with self._lock:
            if self._in_flight <= 0:
                logger.error("auto-loop in_flight 计数下溢")
                self._in_flight = 0
                return
            self._in_flight -= 1

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if self._in_flight <= 0:
                logger.error("auto-loop 完成任务时 in_flight 计数已为 0")
                self._in_flight = 0
            else:
                self._in_flight -= 1
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            if target_reached:
                self._stop_claim_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，正在自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            trigger_break = bool(
                not target_reached
                and self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )
            if trigger_break:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0

        if target_reached:
            logger.info(f"已达目标 {self._target_count} 个成功，停止领取新任务")
            return

        if trigger_break:
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            worker_count = (
                min(self._concurrency, self._target_count)
                if self._target_count else self._concurrency
            )
            for wid in range(worker_count):
                if self._stop_event.is_set() or self._stop_claim_event.is_set():
                    break
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                if wid + 1 < worker_count and self._stop_claim_event.wait(1.0):
                    break
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                if self._target_count and self._registered_ok >= self._target_count:
                    self._last_message = (
                        f"🎯 已达目标 {self._target_count} 个并停止"
                        f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                    )
                else:
                    self._last_message = (
                        f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                    )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        proxy_template = self._proxy_for_worker(worker_id)
        logger.info(f"[worker-{worker_id}] 启动 (proxy={'已配置' if proxy_template else '直连'})")

        while True:
            # 检查停止
            if self._stop_event.is_set() or self._stop_claim_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while (
                    self._pause_event.is_set()
                    and not self._stop_event.is_set()
                    and not self._stop_claim_event.is_set()
                ):
                    time.sleep(0.5)
                if self._stop_event.is_set() or self._stop_claim_event.is_set():
                    return

            # 必须在 claim/start 之前原子预约；成功数 + 在途数不会超过目标数。
            if not self._reserve_slot():
                if self._stop_event.is_set() or self._stop_claim_event.is_set():
                    logger.info(f"[worker-{worker_id}] 已停止领取新任务")
                    return
                time.sleep(0.1)
                continue

            # claim 下一个号（CF 模式用虚拟占位，无需 outlook 号池）
            try:
                mail_source = db.get_setting("mail_source", "outlook")
                if mail_source == "cf_temp":
                    account = {
                        "email": f"cf_placeholder_{int(time.time())}_{worker_id}@cf.local",
                        "password": "", "client_id": "", "refresh_token": "",
                    }
                else:
                    account = db.claim_next()
            except Exception:
                self._release_slot()
                raise
            if not account:
                self._release_slot()
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if (
                        self._stop_event.is_set()
                        or self._stop_claim_event.is_set()
                        or self._pause_event.is_set()
                    ):
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            proxy = proxy_config.pick_working_proxy(proxy_template)
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                self._release_slot()
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if mail_source != "cf_temp":
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            try:
                self._broadcast("state", self._snapshot())
            except Exception as e:
                logger.warning(f"[worker-{worker_id}] 状态快照广播失败: {e}")
            try:
                self._broadcast("run_started", {
                    "worker_id": worker_id,
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                })
            except Exception as e:
                logger.warning(f"[worker-{worker_id}] run_started 广播失败: {e}")

            # 等当前 run 跑完。达标只设置 stop_claim_event，不会中断此等待。
            ok, category = self._wait_run_finish(run_id)

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            try:
                self._broadcast("state", self._snapshot())
            except Exception as e:
                logger.warning(f"[worker-{worker_id}] 状态快照广播失败: {e}")
            try:
                self._broadcast("run_finished", {
                    "worker_id": worker_id,
                    "email": account["email"],
                    "run_id": run_id,
                    "ok": ok,
                    "category": category,
                })
            except Exception as e:
                logger.warning(f"[worker-{worker_id}] run_finished 广播失败: {e}")

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if (
                        self._stop_event.is_set()
                        or self._stop_claim_event.is_set()
                        or self._pause_event.is_set()
                    ):
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str]:
        """轮询 runs 表，等 run 跑完；自动达标不会中断在途任务。"""
        warning_interval = max(1.0, float(timeout))
        next_warning = time.time() + warning_interval
        last_query_warning = 0.0
        while True:
            if self._stop_event.is_set():
                return False, ""
            con = None
            try:
                con = db._conn()
                cur = con.execute(
                    "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
                )
                row = cur.fetchone()
            except Exception as e:
                now = time.time()
                if now - last_query_warning >= 30:
                    logger.warning(f"查询 run {run_id} 状态失败，继续等待: {e}")
                    last_query_warning = now
                time.sleep(1)
                continue
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            now = time.time()
            if now >= next_warning:
                logger.warning(f"run {run_id} 等待超过 {timeout}s，仍非终态，继续跟踪")
                next_warning = now + warning_interval
            time.sleep(1)


# 全局单例
CONTROLLER = AutoLoopController()
