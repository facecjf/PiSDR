"""Band / memory scanner driven by the S-meter.

Quiet channels are stepped through after ``SCAN_DWELL_MS``. When the signal stays above the
active threshold for ``SCAN_HOLD_MS``, scanning pauses so you can listen. After the channel
goes quiet for ``SCAN_RESUME_MS``, scanning continues. HOLD / SKIP / STOP are soft keys.
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
        self._timer = QTimer(self)
        self._timer.setInterval(config.SCAN_TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self.state.scanning

    def start(self, source: str | None = None) -> None:
        if source:
            self.state.set_scan_source(source)
        if self.state.scan_source == "memory" and not self.state.memories:
            self.status.emit("No memories to scan")
            return
        self._manual_hold = False
        self._above_since = None
        self._below_since = None
        self._quiet_since = time.monotonic()
        self.state.set_scan_flags(True, False)
        if not self._timer.isActive():
            self._timer.start()
        label = "band" if self.state.scan_source == "band" else "memory"
        self.status.emit(f"Scan {label}")
        log.info("scan started (%s)", label)

    def stop(self) -> None:
        was = self.state.scanning
        self._timer.stop()
        self._manual_hold = False
        self.state.set_scan_flags(False, False)
        if was:
            self.status.emit("Scan stopped")

    def toggle(self) -> None:
        self.stop() if self.active else self.start()

    def toggle_hold(self) -> None:
        if not self.active:
            return
        self._manual_hold = not self._manual_hold
        self.state.set_scan_flags(True, self._manual_hold or self.state.scan_held)
        self.status.emit("Scan HOLD" if self._manual_hold else "Scan resume")
        if not self._manual_hold:
            self._quiet_since = time.monotonic()
            self._below_since = time.monotonic()

    def skip(self) -> None:
        if not self.active:
            return
        self._manual_hold = False
        self._above_since = None
        self._below_since = None
        self._quiet_since = time.monotonic()
        self.state.set_scan_flags(True, False)
        self._step()
        self.status.emit("Scan skip")

    def cycle_source(self) -> None:
        was = self.active
        if was:
            self.stop()
        self.state.cycle_scan_source()
        if self.state.scan_source == "memory" and not self.state.memories:
            self.state.set_scan_source("band")
            self.status.emit("No memories - band scan")
            if was:
                self.start("band")
            return
        self.status.emit(f"Scan source: {self.state.scan_source}")
        if was:
            self.start()

    def threshold_db(self) -> float:
        return max(self.state.squelch_db, config.SCAN_FLOOR_DB)

    def _tick(self) -> None:
        if not self.state.scanning:
            self._timer.stop()
            return
        if self._manual_hold:
            self.state.set_scan_flags(True, True)
            return

        now = time.monotonic()
        above = self.state.signal_db >= self.threshold_db()

        if above:
            self._below_since = None
            self._quiet_since = None
            if self._above_since is None:
                self._above_since = now
            elif (now - self._above_since) * 1000 >= config.SCAN_HOLD_MS:
                if not self.state.scan_held:
                    self.state.set_scan_flags(True, True)
                    self.status.emit("Scan hold")
            return

        # Below threshold.
        self._above_since = None
        if self.state.scan_held:
            if self._below_since is None:
                self._below_since = now
            elif (now - self._below_since) * 1000 >= config.SCAN_RESUME_MS:
                self.state.set_scan_flags(True, False)
                self._quiet_since = now
                self._below_since = None
                self.status.emit("Scan resume")
            return

        if self._quiet_since is None:
            self._quiet_since = now
        elif (now - self._quiet_since) * 1000 >= config.SCAN_DWELL_MS:
            self._step()
            self._quiet_since = now

    def _step(self) -> None:
        if self.state.scan_source == "memory":
            self._step_memory()
        else:
            self._step_band()

    def _step_band(self) -> None:
        band = self.state.band
        step = self.state.step_hz
        nxt = self.state.vfo_hz + step
        if nxt > band.stop_hz:
            nxt = band.start_hz
            # Snap to the step grid from the band start.
            nxt = int(round(nxt / step) * step)
            if nxt < band.start_hz:
                nxt += step
        self.state.set_vfo(nxt)

    def _step_memory(self) -> None:
        if not self.state.memories:
            self.stop()
            self.status.emit("No memories")
            return
        index = (self.state.memory_index + 1) % len(self.state.memories)
        self.state.recall_memory(index)
