"""Record timer: at a UTC start time, tune the armed frequency and record for N minutes.

Uses the system clock, so the Pi needs network time (or an RTC) for the schedule to be
right. After a one-shot recording the timer disarms itself; a daily timer stays armed.
"""
from __future__ import annotations

import logging
import time

from . import config
from .qt import QObject, QTimer, Signal
from .state import RadioState

log = logging.getLogger(__name__)


class RecTimer(QObject):
    status = Signal(str)
    started = Signal()     # the main window stops the scanner / playback when this fires

    def __init__(self, state: RadioState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._end_t = 0.0
        self._fired_day: int | None = None    # UTC day ordinal of the last start
        self._restore: dict | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(config.REC_TIMER_CHECK_MS)
        self._timer.timeout.connect(self.check)
        self._timer.start()
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field == "recording" and not self.state.recording and self.state.timer_running:
            self._finish(stopped_by_user=True)
        elif field in ("timer_armed", "timer_start_min") and not self.state.timer_running:
            self._fired_day = None
        elif field == "timer_armed" and not self.state.timer_armed and self.state.timer_running:
            self.state.set_recording(False)

    def minutes_until_start(self, now: float | None = None) -> int:
        tm = time.gmtime(now)
        now_min = tm.tm_hour * 60 + tm.tm_min
        return (self.state.timer_start_min - now_min) % (24 * 60)

    def check(self, now: float | None = None) -> None:
        s = self.state
        if s.timer_running:
            if time.monotonic() >= self._end_t:
                self._finish()
            return
        if not s.timer_armed or s.timer_target is None or s.dongle_lent:
            return
        now = time.time() if now is None else now
        tm = time.gmtime(now)
        now_min = tm.tm_hour * 60 + tm.tm_min
        elapsed = (now_min - s.timer_start_min) % (24 * 60)
        if elapsed >= s.timer_length_min:
            return
        start_day = int((now - elapsed * 60) // 86400)
        if self._fired_day == start_day:
            return
        self._fired_day = start_day
        self._begin(remaining_min=s.timer_length_min - elapsed)

    def _begin(self, remaining_min: int) -> None:
        s = self.state
        target = s.timer_target
        self.started.emit()
        restore = {"vfo_hz": s.vfo_hz, "mode": s.mode, "step_hz": s.step_hz}
        s.tune_to(target["vfo_hz"], target["mode"], target["step_hz"])
        if s.recording:
            s.set_recording(False)
        s.set_recording(True)
        if not s.recording:      # card full or unwritable: put the radio back as it was
            s.tune_to(restore["vfo_hz"], restore["mode"], restore["step_hz"])
            log.warning("record timer: recording could not start")
            return
        self._restore = restore
        self._end_t = time.monotonic() + remaining_min * 60
        s.set_timer_running(True)
        self.status.emit(f"Timer recording {target['name']} for {remaining_min} min")
        log.info("record timer started: %s for %d min", target["name"], remaining_min)

    def _finish(self, stopped_by_user: bool = False) -> None:
        s = self.state
        s.set_timer_running(False)
        if s.recording:
            s.set_recording(False)
        if not s.timer_daily:
            s.arm_timer(False)
        restore, self._restore = self._restore, None
        if restore and not stopped_by_user:
            s.tune_to(restore["vfo_hz"], restore["mode"], restore["step_hz"])
        self.status.emit("Timer recording stopped" if stopped_by_user else "Timer recording finished")
