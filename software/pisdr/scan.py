"""Band / memory scanner driven by the S-meter.

Quiet channels are stepped through after the dwell time. When the signal stays above the
active threshold for ``SCAN_HOLD_MS``, scanning pauses so you can listen and the hit is
logged. After the channel goes quiet for the resume time, scanning continues (resume time
0 = stay until SKIP / RESUME). Locked-out frequencies are skipped. Memory scan follows the
memory group selected on the memories page and, optionally, only memories "on now".

Priority: while scanning, every few seconds the scanner hops to the priority channel for
``PRIORITY_LOOK_MS``. If it is active, the scanner holds there; otherwise it hops back.
"""
from __future__ import annotations

import logging
import time

from . import config
from .qt import QObject, QTimer, Signal
from .state import RadioState

log = logging.getLogger(__name__)


class Scanner(QObject):
    status = Signal(str)

    def __init__(self, state: RadioState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._manual_hold = False
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._quiet_since: float | None = None
        self._ignore_until = 0.0
        self._hit: dict | None = None
        self._hit_t0 = 0.0
        self._next_prio = 0.0
        self._prio_return: dict | None = None   # where to go back to after a priority visit
        self._prio_t0 = 0.0
        self._on_priority = False              # holding on an active priority channel
        self._timer = QTimer(self)
        self._timer.setInterval(config.SCAN_TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self.state.scanning

    def _memory_candidates(self) -> list[int]:
        s = self.state
        return [i for i in s.memory_indices(on_now_only=s.memory_scan_on_now)
                if not s.is_locked_out(s.memories[i].vfo_hz)]

    def start(self, source: str | None = None) -> None:
        if source:
            self.state.set_scan_source(source)
        if self.state.scan_source == "memory" and not self._memory_candidates():
            self.status.emit("No memories to scan (check group / ON NOW / lockouts)")
            return
        now = time.monotonic()
        self._manual_hold = False
        self._above_since = None
        self._below_since = None
        self._quiet_since = now
        self._prio_return = None
        self._on_priority = False
        self._next_prio = now + self.state.priority_interval_s
        self.state.set_scan_flags(True, False)
        if not self._timer.isActive():
            self._timer.start()
        label = "band" if self.state.scan_source == "band" else f"memory {self.state.memory_group}"
        self.status.emit(f"Scan {label}")
        log.info("scan started (%s)", label)

    def stop(self) -> None:
        was = self.state.scanning
        self._timer.stop()
        self._end_hit()
        if self._prio_return is not None and not self._on_priority:
            self._go_back(time.monotonic())     # stopped mid-look: return to where we were
        self._prio_return = None
        self._on_priority = False
        self._manual_hold = False
        self.state.set_scan_flags(False, False)
        if was:
            self.status.emit("Scan stopped")

    def toggle(self) -> None:
        self.stop() if self.active else self.start()

    def toggle_hold(self) -> None:
        """HOLD / RESUME key. Resuming from an automatic hold moves on to the next channel."""
        if not self.active:
            return
        if self.state.scan_held and not self._manual_hold:
            self.skip()
            return
        # An automatic hold was handled above, so the held flag here is only ever our own.
        self._manual_hold = not self._manual_hold
        self.state.set_scan_flags(True, self._manual_hold)
        self.status.emit("Scan HOLD" if self._manual_hold else "Scan resume")
        if not self._manual_hold:
            self._quiet_since = time.monotonic()
            self._above_since = None
            self._below_since = None

    def skip(self) -> None:
        if not self.active:
            return
        now = time.monotonic()
        self._end_hit()
        self._manual_hold = False
        self._above_since = None
        self._below_since = None
        self._quiet_since = now
        self.state.set_scan_flags(True, False)
        if self._prio_return is not None:
            self._go_back(now)
        self._step()
        self.status.emit("Scan skip")

    def lockout_current(self) -> None:
        """Lock out the channel the scanner is on and move on."""
        if not self.active:
            return
        hz = self.state.vfo_hz
        if self._prio_return is not None:
            self.status.emit("Priority channel can't be locked out")
            return
        self.state.lock_out(hz)
        self.skip()
        self.status.emit(f"Locked out {hz / 1e6:.4f} MHz")

    def cycle_source(self) -> None:
        was = self.active
        if was:
            self.stop()
        self.state.cycle_scan_source()
        if self.state.scan_source == "memory" and not self._memory_candidates():
            self.state.set_scan_source("band")
            self.status.emit("No memories - band scan")
            if was:
                self.start("band")
            return
        self.status.emit(f"Scan source: {self.state.scan_source}")
        if was:
            self.start()

    def threshold_db(self) -> float:
        return max(self.state.squelch_db, self.state.scan_floor_db)

    # ------------------------------------------------------------------ main loop
    def _tick(self) -> None:
        if not self.state.scanning:
            self._timer.stop()
            return
        now = time.monotonic()
        if self._prio_return is not None and not self._on_priority:
            self._tick_priority_look(now)
            return
        if self._manual_hold:
            self.state.set_scan_flags(True, True)
            return
        if now < self._ignore_until:
            return

        level = self.state.signal_db
        if level >= self.threshold_db():
            self._below_since = None
            self._quiet_since = None
            if self._hit is not None:
                self._hit["peak_db"] = max(self._hit["peak_db"], round(level, 1))
            if self._above_since is None:
                self._above_since = now
            elif (now - self._above_since) * 1000 >= config.SCAN_HOLD_MS and not self.state.scan_held:
                self.state.set_scan_flags(True, True)
                self._begin_hit("memory" if self.state.scan_source == "memory" else "band")
                self.status.emit("Scan hold")
            return

        # Below threshold.
        self._above_since = None
        if self.state.scan_held:
            resume_ms = self.state.scan_resume_ms
            if self._below_since is None:
                self._below_since = now
            elif resume_ms > 0 and (now - self._below_since) * 1000 >= resume_ms:
                self._end_hit()
                self.state.set_scan_flags(True, False)
                self._quiet_since = now
                self._below_since = None
                if self._on_priority:
                    self._go_back(now)
                self.status.emit("Scan resume")
            return

        if self._priority_due(now):
            self._start_priority_look(now)
            return
        if self._quiet_since is None:
            self._quiet_since = now
        elif (now - self._quiet_since) * 1000 >= self.state.scan_dwell_ms:
            self._step()
            self._quiet_since = now

    # ------------------------------------------------------------------ stepping
    def _step(self) -> None:
        if self.state.scan_source == "memory":
            self._step_memory()
        else:
            self._step_band()

    def _step_band(self) -> None:
        band = self.state.band
        step = self.state.step_hz
        channels = max(1, (band.stop_hz - band.start_hz) // step + 1)
        nxt = self.state.vfo_hz
        for _ in range(channels):
            nxt += step
            if nxt > band.stop_hz:
                # Wrap, snapped to the step grid from the band start.
                nxt = int(round(band.start_hz / step) * step)
                if nxt < band.start_hz:
                    nxt += step
            if not self.state.is_locked_out(nxt):
                self.state.set_vfo(nxt)
                return
        self.stop()
        self.status.emit("Every channel in this band is locked out")

    def _step_memory(self) -> None:
        candidates = self._memory_candidates()
        if not candidates:
            self.stop()
            self.status.emit("No memories to scan")
            return
        current = self.state.memory_index
        index = next((i for i in candidates if i > current), candidates[0])
        self.state.recall_memory(index, announce=False)

    # ------------------------------------------------------------------ priority
    def _priority_due(self, now: float) -> bool:
        p = self.state.priority
        if not p or now < self._next_prio:
            return False
        if p["vfo_hz"] == self.state.vfo_hz and p["mode"] == self.state.mode:
            self._next_prio = now + self.state.priority_interval_s
            return False
        return True

    def _start_priority_look(self, now: float) -> None:
        s = self.state
        p = s.priority
        self._prio_return = {"vfo_hz": s.vfo_hz, "mode": s.mode, "step_hz": s.step_hz,
                             "memory_index": s.memory_index}
        self._prio_t0 = now
        self._above_since = None
        self._ignore_until = now + config.PRIORITY_SETTLE_MS / 1000
        s.tune_to(p["vfo_hz"], p["mode"], p["step_hz"])

    def _tick_priority_look(self, now: float) -> None:
        if now < self._ignore_until:
            return
        if self.state.signal_db >= self.threshold_db():
            if self._above_since is None:
                self._above_since = now
            elif (now - self._above_since) * 1000 >= config.SCAN_HOLD_MS:
                self._on_priority = True
                self._below_since = None
                self.state.set_scan_flags(True, True)
                self._begin_hit("priority")
                self.status.emit(f"Priority: {self.state.priority['name']}")
                return
        else:
            self._above_since = None
        if (now - self._prio_t0) * 1000 >= config.PRIORITY_LOOK_MS + config.PRIORITY_SETTLE_MS:
            self._go_back(now)

    def _go_back(self, now: float) -> None:
        back, self._prio_return = self._prio_return, None
        self._on_priority = False
        self._next_prio = now + self.state.priority_interval_s
        self._above_since = None
        self._quiet_since = now
        if back is None:
            return
        self.state.tune_to(back["vfo_hz"], back["mode"], back["step_hz"])
        self.state.memory_index = back["memory_index"]
        self._ignore_until = now + config.PRIORITY_SETTLE_MS / 1000

    # ------------------------------------------------------------------ hit log
    def _begin_hit(self, source: str) -> None:
        s = self.state
        self._end_hit()
        if source == "priority" and s.priority:
            name = s.priority["name"]
        else:
            name = s.name_for(s.vfo_hz, s.mode)
        self._hit = {"t": int(time.time()), "vfo_hz": s.vfo_hz, "mode": s.mode, "name": name,
                     "peak_db": round(s.signal_db, 1), "src": source}
        self._hit_t0 = time.monotonic()

    def _end_hit(self) -> None:
        hit, self._hit = self._hit, None
        if hit is None:
            return
        hit["dur_s"] = round(time.monotonic() - self._hit_t0, 1)
        self.state.add_scan_hit(hit)
