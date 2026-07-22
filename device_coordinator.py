"""Priority-aware access coordinator for the Ultimate's small network stack.

The Ultimate's REST and FTP services share limited firmware resources.  This
coordinator serialises device operations and lets user-initiated work jump
in front of background indexing without killing the index job.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class OperationCancelled(RuntimeError):
    """Raised when a waiting background operation is cancelled."""


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
        self._manual_paused = False
        self._manual_pause_reason = "paused by user"
        self._local = threading.local()
        self._completed = 0
        self._total_wait = 0.0

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

    @contextmanager
    def operation(
        self,
        priority: str = "interactive",
        reason: str = "device operation",
        *,
        wait_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        """Acquire the device transport at the requested priority.

        ``wait_callback`` receives the reason while blocked and an empty string
        once acquired. ``cancel_check`` is useful for a background index that
        may be stopped while waiting behind an interactive operation.
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
        with self._cond:
            self._waiting[number] += 1
            try:
                while not self._can_start(number):
                    if cancel_check and cancel_check():
                        raise OperationCancelled("device operation cancelled")
                    if wait_callback:
                        wait_callback(self._blocked_reason(number))
                    self._cond.wait(timeout=0.10)
                if cancel_check and cancel_check():
                    raise OperationCancelled("device operation cancelled")
                self._active = True
                self._active_priority = priority
                self._active_reason = reason
                self._active_owner = threading.get_ident()
                self._local.depth = 1
                self._local.priority = priority
                self._total_wait += max(0.0, time.monotonic() - start_wait)
            finally:
                self._waiting[number] -= 1
            if wait_callback:
                wait_callback("")

        try:
            yield
        finally:
            with self._cond:
                self._local.depth = 0
                self._local.priority = None
                self._active = False
                self._active_priority = None
                self._active_reason = ""
                self._active_owner = None
                self._completed += 1
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
            )
