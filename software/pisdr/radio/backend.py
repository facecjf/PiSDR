"""Backend interface shared by the GNU Radio flowgraph and the simulator.

A backend owns the hardware centre frequency. The UI only ever talks in terms of the VFO
frequency; the backend decides when to retune the dongle so the VFO stays inside the
visible span and away from the DC spike, and announces that via :attr:`center_changed`.
"""
from __future__ import annotations

import logging

import numpy as np

from .. import config
from ..qt import QObject, Signal
from ..state import RadioState

log = logging.getLogger(__name__)


class Backend(QObject):
    """Abstract receiver. Subclasses implement the ``_hw_*`` hooks."""

    spectrum_ready = Signal(object)        # np.ndarray of dB values, length FFT_SIZE, ascending freq
    center_changed = Signal(float, float)  # (center_hz, span_hz)
    status = Signal(str)

    #: fraction of the span the VFO may wander from centre before the hardware retunes
    RETUNE_FRACTION = 0.42
    #: where the VFO is parked relative to centre after a retune (keeps it off the DC spike)
    PARK_FRACTION = 0.25

    def __init__(self, state: RadioState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.span_hz = float(config.SAMPLE_RATE)
        self.center_hz: float = 0.0
        self.running = False
        state.changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.running = True
        self._hw_start()
        self._apply_all()

    def stop(self) -> None:
        self.running = False
        if self.state.recording:
            self._hw_set_recording(False)
        self._hw_stop()

    def _apply_all(self) -> None:
        s = self.state
        self._retune_for_vfo(force=True)
        self._hw_set_mode(s.mode)
        self._hw_set_volume(s.volume)
        self._hw_set_squelch(s.squelch_db)
        self._hw_set_gain(s.gain_db, s.agc)
        self._hw_set_fft_fps(s.fft_fps)

    # ------------------------------------------------------------------ state glue
    def _on_state_changed(self, field: str) -> None:
        if not self.running:
            return
        s = self.state
        if field == "vfo_hz":
            self._retune_for_vfo()
        elif field == "mode":
            self._hw_set_mode(s.mode)
            self._hw_set_offset(self.vfo_offset_hz())
        elif field == "volume":
            self._hw_set_volume(s.volume)
        elif field == "squelch_db":
            self._hw_set_squelch(s.squelch_db)
        elif field in ("gain_db", "agc"):
            self._hw_set_gain(s.gain_db, s.agc)
        elif field == "bias_tee":
            self._hw_set_bias_tee(s.bias_tee)
        elif field == "recording":
            self._hw_set_recording(s.recording)
        elif field == "fft_fps":
            self._hw_set_fft_fps(s.fft_fps)

    def vfo_offset_hz(self) -> float:
        return self.state.vfo_hz - self.center_hz

    def _retune_for_vfo(self, force: bool = False) -> None:
        vfo = float(self.state.vfo_hz)
        if force or abs(vfo - self.center_hz) > self.span_hz * self.RETUNE_FRACTION:
            park = self.span_hz * self.PARK_FRACTION
            # Park the VFO a quarter span above centre, unless that would put the lower
            # edge of the span below 0 Hz, in which case park it below centre instead.
            center = vfo - park
            if center < self.span_hz / 2:
                center = vfo + park
            self.center_hz = center
            self._hw_set_center(center)
            self.center_changed.emit(self.center_hz, self.span_hz)
        self._hw_set_offset(self.vfo_offset_hz())

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None: ...
    def _hw_stop(self) -> None: ...
    def _hw_set_center(self, hz: float) -> None: ...
    def _hw_set_offset(self, hz: float) -> None: ...
    def _hw_set_mode(self, mode: str) -> None: ...
    def _hw_set_volume(self, pct: int) -> None: ...
    def _hw_set_squelch(self, db: float) -> None: ...
    def _hw_set_gain(self, db: float, agc: bool) -> None: ...
    def _hw_set_bias_tee(self, on: bool) -> None: ...
    def _hw_set_recording(self, on: bool) -> None: ...
    def _hw_set_fft_fps(self, fps: int) -> None: ...

    # ------------------------------------------------------------------ utilities
    @staticmethod
    def power_db(spectrum: np.ndarray) -> np.ndarray:
        return 10.0 * np.log10(np.maximum(spectrum, 1e-20))


def make_backend(state: RadioState, force_sim: bool = False) -> Backend:
    """Return the GNU Radio backend when it can be imported, else the simulator."""
    reason = "--sim requested"
    if not force_sim:
        try:
            from .flowgraph import GnuRadioBackend  # noqa: WPS433 (import inside function on purpose)
            backend = GnuRadioBackend(state)
            log.info("Using GNU Radio backend")
            return backend
        except Exception as exc:  # ImportError or device open failure
            log.exception("GNU Radio backend unavailable; using simulator")
            reason = f"{type(exc).__name__}: {exc}"
    from .backend_sim import SimBackend
    return SimBackend(state, reason=reason)
