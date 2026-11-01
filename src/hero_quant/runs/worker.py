"""runs.worker — 队列消费者（PR-203）。

契约（docs/contracts/batch-14.md §5）：

- `handler(run, ctx)`；`ctx.is_cancelled() -> bool`；`with ctx.step("name") as step:`
  进入时查 `run_steps`，已完成则跳过并置 `step.skipped=True`。
- **短事务**：领取一次事务、handler 在事务外跑（LLM 调用绝不包事务）、写结果再一次事务。
- **心跳**：每 `heartbeat_interval` 续租；续租被 fencing 立即标记租约丢失，handler 在下一个
  step 边界收到 `RunCancelled`（不承诺打断进行中的单次 LLM 调用）。
- **协作式取消**：只在 step 边界检查 `status='cancelling'` 并转 `cancelled`；
  验收口径是「step 边界 5s 内生效」。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from hero_quant.runs.steps import StepLedger
from hero_quant.runs.store import MAX_ID_LEN, RunQueue, RunsError, _text

logger = logging.getLogger("hero_quant.runs")

DEFAULT_CANCEL_POLL_SECONDS = 1.0
_HEARTBEAT_JOIN_TIMEOUT = 5.0


class RunCancelled(RuntimeError):
    """step 边界观察到的取消/租约丢失；handler 可捕获做清理，但不该吞掉。"""

    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = reason
        super().__init__(f"run_cancelled: {reason}")


class Step:
    """一个 step 的句柄：`skipped` 表示断点恢复命中，`output` 供 handler 写结果。"""

    def __init__(self, name: str, *, skipped: bool = False, output: Any = None) -> None:
        self.name = name
        self.skipped = skipped
        self.output = output

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Step(name={self.name!r}, skipped={self.skipped!r})"


class RunContext:
    """交给 handler 的上下文；只暴露契约里的 is_cancelled / step。"""

    def __init__(
        self,
        queue: RunQueue,
        run: dict[str, Any],
        *,
        cancel_poll_seconds: float = DEFAULT_CANCEL_POLL_SECONDS,
    ) -> None:
        self._queue = queue
        self._run = run
        self._steps = StepLedger(queue.pool, tenant_id=str(run["tenant_id"]))
        self._done: dict[str, Any] | None = None
        self._lease_lost = threading.Event()
        self._cancel_poll_seconds = max(0.0, float(cancel_poll_seconds))
        self._last_probe = 0.0
        self._last_status = ""
        payload = run.get("payload") if isinstance(run.get("payload"), dict) else {}
        hint = payload.get("total_steps")
        self.total_steps: int | None = int(hint) if isinstance(hint, int) and hint > 0 else None

    # ------------------------------------------------------------------ 只读
    @property
    def run_id(self) -> str:
        return str(self._run["id"])

    @property
    def tenant_id(self) -> str:
        return str(self._run["tenant_id"])

    @property
    def attempt(self) -> int:
        return int(self._run["attempt"])

    @property
    def lease_lost(self) -> bool:
        """心跳续租被 fencing：本 worker 的所有写入都会被拒绝，应尽快停。"""
        return self._lease_lost.is_set()

    def _mark_lease_lost(self) -> None:
        self._lease_lost.set()

    def is_cancelled(self) -> bool:
        """租约丢失或库中 status='cancelling' 即视为应停（step 边界调用）。"""
        if self._lease_lost.is_set():
            return True
        now = time.monotonic()
        if self._last_status and now - self._last_probe < self._cancel_poll_seconds:
            return self._last_status == "cancelling"
        self._last_probe = now
        self._last_status = self._queue._status(self.tenant_id, self.run_id)
        return self._last_status == "cancelling"

    # ------------------------------------------------------------------ step
    @contextmanager
    def step(self, name: str) -> Iterator[Step]:
        """进入时判取消 + 查断点；正常退出写 succeeded，异常退出写 failed 后原样抛出。"""
        label = _text(name, "step_name", MAX_ID_LEN)
        if self.is_cancelled():
            raise RunCancelled("lease_lost" if self._lease_lost.is_set() else "cancelled")
        if self._done is None:
            self._done = self._steps._done_outputs(self.run_id)
        if label in self._done:
            yield Step(label, skipped=True, output=self._done[label])
            return
        handle = Step(label)
        try:
            yield handle
        except Exception as exc:
            # 不写 failed step 行：run_steps 是「已完成工作」台账，只有 succeeded 参与断点
            # 跳过。写 failed 行会让后续 attempt 因 ON CONFLICT DO NOTHING 无法记录该 step
            # 的成功，出现「run 成功但 step 仍 failed」的假象。失败信息走 runs.error +
            # 这条日志（带 step 名，便于定位）。
            logger.warning(
                "runs.step_failed run_id=%s step=%s attempt=%s error_type=%s",
                self.run_id,
                label,
                self.attempt,
                type(exc).__name__,
            )
            raise
        self._steps.record(
            self.run_id,
            self.tenant_id,
            label,
            status="succeeded",
            output=handle.output,
            attempt=self.attempt,
        )


class RunWorker:
    """单进程消费者；`run_once()` 处理一个任务，`run_forever()` 轮询。"""

    def __init__(
        self,
        queue: RunQueue,
        handler: Callable[[dict[str, Any], RunContext], Any],
        *,
        lease_seconds: int = 300,
        heartbeat_interval: float = 30.0,
        poll_interval: float = 1.0,
    ) -> None:
        if not callable(handler):
            raise ValueError("handler must be callable")
        # 鸭子类型而不是 isinstance：worker 只需要 claim/finish/fail/heartbeat 这套契约，
        # 单测用假 queue 时不必伪造整个 RunQueue。
        for method in ("claim", "finish", "fail", "heartbeat", "settle_cancel"):
            if not callable(getattr(queue, method, None)):
                raise ValueError(f"queue must provide .{method}()")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be > 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        self._queue = queue
        self._handler = handler
        self._lease_seconds = int(lease_seconds)
        self._heartbeat_interval = float(heartbeat_interval)
        self._poll_interval = float(poll_interval)
        self._stop = threading.Event()

    @property
    def queue(self) -> RunQueue:
        return self._queue

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """领取并处理一个任务；该轮没抢到任务返回 False。"""
        run = self._queue.claim(lease_seconds=self._lease_seconds)
        if run is None:
            return False
        self._execute(run)
        return True

    def run_forever(self) -> None:
        """轮询直到 request_stop()；单次失败只记录，不让线程整体退出。"""
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception as exc:  # noqa: BLE001 - 消费者不能因单任务异常整体退出
                logger.error(
                    "runs.worker_loop_failed error_type=%s error_code=runs.worker_loop_failed",
                    type(exc).__name__,
                    exc_info=True,
                )
            if not worked:
                self._stop.wait(self._poll_interval)

    # ------------------------------------------------------------------ 内部
    def _execute(self, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        tenant = str(run["tenant_id"])
        attempt = int(run["attempt"])
        ctx = RunContext(self._queue, run, cancel_poll_seconds=self._poll_interval)
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(ctx, stop),
            name=f"runs-heartbeat-{run_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            try:
                result = self._handler(run, ctx)
            except RunCancelled as exc:
                # 取消/租约丢失都走这里：不再写入结果，只把 cancelling 收成 cancelled。
                logger.info("runs.cancelled_at_boundary run_id=%s reason=%s", run_id, exc.reason)
                if not ctx.lease_lost:
                    self._queue.settle_cancel(tenant, run_id, attempt)
            except Exception as exc:  # noqa: BLE001 - handler 异常必须落 fail，不能吞
                logger.error(
                    "runs.handler_failed run_id=%s attempt=%s error_type=%s"
                    " error_code=runs.handler_failed",
                    run_id,
                    attempt,
                    type(exc).__name__,
                    exc_info=True,
                )
                self._queue.fail(run_id, attempt, error=f"{type(exc).__name__}: {exc}")
            else:
                self._settle_success(run_id, tenant, attempt, ctx, result)
        finally:
            stop.set()
            heartbeat.join(timeout=_HEARTBEAT_JOIN_TIMEOUT)
            self._queue._forget(run_id, attempt)

    def _settle_success(
        self, run_id: str, tenant: str, attempt: int, ctx: RunContext, result: Any
    ) -> None:
        if ctx.lease_lost:
            # 租约已丢：写也写不进（会被 fencing），把结果丢掉并留痕，等看门狗重排。
            logger.warning("runs.result_dropped_lease_lost run_id=%s attempt=%s", run_id, attempt)
            return
        if ctx.is_cancelled():
            self._queue.settle_cancel(tenant, run_id, attempt)
            return
        if self._queue.finish(run_id, attempt, result=result):
            return
        # finish 被拒：可能是取消在最后一刻生效（status 变 cancelling），补一次收口；
        # 真被 fencing 的话这条同样 0 行，不会覆盖别人的结果。
        self._queue.settle_cancel(tenant, run_id, attempt)

    def _heartbeat_loop(self, ctx: RunContext, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_interval):
            try:
                renewed = self._queue.heartbeat(
                    ctx.run_id, ctx.attempt, lease_seconds=self._lease_seconds
                )
            except RunsError as exc:
                logger.warning(
                    "runs.heartbeat_unavailable run_id=%s reason=%s", ctx.run_id, exc.reason
                )
                continue
            if not renewed:
                ctx._mark_lease_lost()
                return


__all__ = ["RunCancelled", "RunContext", "RunWorker", "Step"]
