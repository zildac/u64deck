"""Priority-aware access coordinator for the Ultimate's small network stack.

The Ultimate's REST and FTP services share limited firmware resources.  This
coordinator serialises device operations and lets user-initiated work jump
in front of background indexing without killing the index job.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class OperationCancelled(RuntimeError):
    """Raised when a waiting background operation is cancelled."""


class OperationExpired(RuntimeError):
    """Raised when a time-sensitive queued operation is no longer useful."""


@dataclass(frozen=True)
class CoordinatorSnapshot:
    active_priority: str | None
    active_reason: str
    waiting_interactive: int
    waiting_status: int
    waiting_background: int
    manual_paused: bool
    manual_pause_reason: str
    completed: int
    total_wait_seconds: float
    active_seconds: float
    longest_wait_seconds: float
    average_wait_seconds: float
    p95_wait_seconds: float
    completed_interactive: int
    completed_status: int
    completed_background: int
    cancelled: int
    expired: int


class DeviceOperationCoordinator:
    """Serialise Ultimate operations using three priority levels.

    Priority order is interactive > status > background.  The coordinator is
    re-entrant per thread so a route can establish a priority and lower-level
    REST/FTP methods can safely enter it again without deadlocking.
    """

    PRIORITIES = {"interactive": 0, "status": 1, "background": 2}

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._waiting = [0, 0, 0]
        self._active = False
        self._active_priority: str | None = None
        self._active_reason = ""
        self._active_owner: int | None = None
        self._active_started = 0.0
        self._manual_paused = False
        self._manual_pause_reason = "paused by user"
        self._local = threading.local()
        self._completed = 0
        self._total_wait = 0.0
        self._wait_samples = deque(maxlen=240)
        self._completed_by_priority = {name: 0 for name in self.PRIORITIES}
        self._cancelled = 0
        self._expired = 0
        self._history = deque(maxlen=40)

    def _priority_number(self, priority: str) -> int:
        try:
            return self.PRIORITIES[priority]
        except KeyError as exc:
            raise ValueError(f"unknown device priority: {priority}") from exc

    def _higher_waiting(self, number: int) -> bool:
        return any(self._waiting[p] for p in range(number))

    def _can_start(self, number: int) -> bool:
        if self._active:
            return False
        if self._higher_waiting(number):
            return False
        if number == self.PRIORITIES["background"] and self._manual_paused:
            return False
        return True

    def _blocked_reason(self, number: int) -> str:
        if number == self.PRIORITIES["background"] and self._manual_paused:
            return self._manual_pause_reason
        if self._active:
            return self._active_reason or f"{self._active_priority or 'device'} operation"
        for priority, pnum in self.PRIORITIES.items():
            if pnum < number and self._waiting[pnum]:
                return f"waiting for {priority} operation"
        return "waiting for device"

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(float(value) for value in samples)
        rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
        return ordered[rank]

    def _record_history(self, *, priority: str, reason: str, wait_seconds: float,
                        duration_seconds: float, outcome: str, error: str = "",
                        started_at: float | None = None) -> None:
        self._history.append({
            "started_at": float(started_at or time.time()),
            "finished_at": time.time(),
            "priority": priority,
            "reason": reason,
            "wait_seconds": round(max(0.0, wait_seconds), 4),
            "duration_seconds": round(max(0.0, duration_seconds), 4),
            "outcome": outcome,
            "error": str(error)[:300],
        })

    @contextmanager
    def operation(
        self,
        priority: str = "interactive",
        reason: str = "device operation",
        *,
        wait_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        max_wait_seconds: float | None = None,
    ) -> Iterator[None]:
        """Acquire the device transport at the requested priority.

        ``wait_callback`` receives the reason while blocked and an empty string
        once acquired. ``cancel_check`` is useful for a background index that
        may be stopped while waiting behind an interactive operation.

        ``max_wait_seconds`` is deliberately opt-in. It is intended only for
        time-sensitive manual actions, such as a screen-mirror keypress or a
        Reset request, which would be dangerous if delivered against a later
        machine state. Existing callers retain unlimited waiting by default.
        """
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        number = self._priority_number(priority)
        start_wait = time.monotonic()
        started_wall = time.time()
        acquired_at = 0.0
        wait_seconds = 0.0
        try:
            with self._cond:
                self._waiting[number] += 1
                try:
                    deadline = None
                    if max_wait_seconds is not None:
                        deadline = start_wait + max(0.0, float(max_wait_seconds))
                    while not self._can_start(number):
                        if cancel_check and cancel_check():
                            raise OperationCancelled("device operation cancelled")
                        now = time.monotonic()
                        if deadline is not None and now >= deadline:
                            blocked = self._blocked_reason(number)
                            raise OperationExpired(
                                f"{reason} expired while waiting for {blocked}"
                            )
                        if wait_callback:
                            wait_callback(self._blocked_reason(number))
                        remaining = max(0.0, deadline - now) if deadline is not None else 0.10
                        self._cond.wait(timeout=min(0.10, remaining) if deadline is not None else 0.10)
                    if cancel_check and cancel_check():
                        raise OperationCancelled("device operation cancelled")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise OperationExpired(f"{reason} expired before delivery")
                    acquired_at = time.monotonic()
                    wait_seconds = max(0.0, acquired_at - start_wait)
                    self._active = True
                    self._active_priority = priority
                    self._active_reason = reason
                    self._active_owner = threading.get_ident()
                    self._active_started = acquired_at
                    self._local.depth = 1
                    self._local.priority = priority
                    self._total_wait += wait_seconds
                    self._wait_samples.append(wait_seconds)
                finally:
                    self._waiting[number] -= 1
                if wait_callback:
                    wait_callback("")
        except (OperationCancelled, OperationExpired) as exc:
            with self._cond:
                outcome = "expired" if isinstance(exc, OperationExpired) else "cancelled"
                if outcome == "expired":
                    self._expired += 1
                else:
                    self._cancelled += 1
                self._record_history(
                    priority=priority, reason=reason,
                    wait_seconds=max(0.0, time.monotonic() - start_wait),
                    duration_seconds=0.0, outcome=outcome, error=str(exc),
                    started_at=started_wall,
                )
                self._cond.notify_all()
            raise

        outcome = "ok"
        error = ""
        try:
            yield
        except BaseException as exc:
            outcome = "error"
            error = str(exc)
            raise
        finally:
            finished = time.monotonic()
            duration = max(0.0, finished - acquired_at) if acquired_at else 0.0
            with self._cond:
                self._local.depth = 0
                self._local.priority = None
                self._active = False
                self._active_priority = None
                self._active_reason = ""
                self._active_owner = None
                self._active_started = 0.0
                self._completed += 1
                self._completed_by_priority[priority] += 1
                self._record_history(
                    priority=priority, reason=reason, wait_seconds=wait_seconds,
                    duration_seconds=duration, outcome=outcome, error=error,
                    started_at=started_wall,
                )
                self._cond.notify_all()

    def set_background_paused(self, paused: bool, reason: str = "paused by user") -> None:
        with self._cond:
            self._manual_paused = bool(paused)
            if reason:
                self._manual_pause_reason = reason
            self._cond.notify_all()

    def background_paused(self) -> bool:
        with self._cond:
            return self._manual_paused

    def wake(self) -> None:
        """Wake waiters so they can re-check stop/cancel conditions."""
        with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> CoordinatorSnapshot:
        with self._cond:
            samples = list(self._wait_samples)
            average = (sum(samples) / len(samples)) if samples else 0.0
            return CoordinatorSnapshot(
                active_priority=self._active_priority,
                active_reason=self._active_reason,
                waiting_interactive=self._waiting[self.PRIORITIES["interactive"]],
                waiting_status=self._waiting[self.PRIORITIES["status"]],
                waiting_background=self._waiting[self.PRIORITIES["background"]],
                manual_paused=self._manual_paused,
                manual_pause_reason=self._manual_pause_reason,
                completed=self._completed,
                total_wait_seconds=round(self._total_wait, 3),
                active_seconds=round(max(0.0, time.monotonic() - self._active_started), 3)
                if self._active and self._active_started else 0.0,
                longest_wait_seconds=round(max(samples), 3) if samples else 0.0,
                average_wait_seconds=round(average, 3),
                p95_wait_seconds=round(self._percentile(samples, 0.95), 3),
                completed_interactive=self._completed_by_priority["interactive"],
                completed_status=self._completed_by_priority["status"],
                completed_background=self._completed_by_priority["background"],
                cancelled=self._cancelled,
                expired=self._expired,
            )

    def recent_operations(self, limit: int = 20) -> list[dict]:
        with self._cond:
            rows = list(self._history)[-max(0, int(limit)):]
            return [dict(row) for row in rows]
