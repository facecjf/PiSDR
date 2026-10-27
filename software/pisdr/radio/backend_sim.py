"""Simulated receiver for developing the UI without any hardware.

Produces a plausible spectrum: a noise floor that rises toward HF, a handful of
deterministic "stations" scattered across every 1.92 MHz block so there is always
something to tune to, a DC spike where the dongle would have one, and a slowly fading
carrier wherever the VFO happens to be so the S-meter moves. No audio is generated.
"""
from __future__ import annotations

import math
import time

import numpy as np

from .. import config
from ..qt import QTimer
from .backend import Backend


class SimBackend(Backend):
    def __init__(self, state, parent=None, reason: str = "") -> None:
        super().__init__(state, parent)
        self.reason = reason
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._offset_hz = 0.0
        self._gain_db = config.DEFAULT_GAIN_DB
        self._mode = state.mode
        self._rng = np.random.default_rng(1)
        self._bins = np.arange(config.FFT_SIZE)
        self._t0 = time.monotonic()
        self._stations: list[tuple[float, float, float, str]] = []  # (hz, level_db, bw_hz, kind)

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None:
        self._timer.start(int(1000 / self.state.fft_fps))
        if self.reason and self.reason != "--sim requested":
            self.status.emit(f"SIMULATOR - {self.reason[:70]} (run: python3 -m pisdr --check)")
        else:
            self.status.emit("SIMULATOR - no radio hardware")

    def _hw_stop(self) -> None:
        self._timer.stop()

    def _hw_set_center(self, hz: float) -> None:
        self._stations = self._make_stations(hz)

    def _hw_set_offset(self, hz: float) -> None:
        self._offset_hz = hz

    def _hw_set_mode(self, mode: str) -> None:
        self._mode = mode

    def _hw_set_gain(self, db: float, agc: bool) -> None:
        self._gain_db = 35.0 if agc else db

    def _hw_set_fft_fps(self, fps: int) -> None:
        if self._timer.isActive():
            self._timer.start(int(1000 / fps))

    def _hw_set_recording(self, on: bool) -> None:
        self.status.emit("Recording (simulated)" if on else "Recording stopped")

    # ------------------------------------------------------------------ synthesis
    def _make_stations(self, center_hz: float) -> list[tuple[float, float, float, str]]:
        """Deterministic pseudo-stations for the 1.92 MHz block around *center_hz*."""
        block = int(center_hz // self.span_hz)
        rng = np.random.default_rng(block)
        stations = []
        for _ in range(rng.integers(4, 9)):
            hz = (block - 0.5 + 2.0 * rng.random()) * self.span_hz
            hz = round(hz / 5000) * 5000
            level = -75 + 35 * rng.random()
            if hz > 87e6 and hz < 108e6:
                bw = 150e3
                kind = "wfm"
            elif hz > 30e6:
                bw = 10e3 + 5e3 * rng.random()
                kind = "nfm"
            else:
                bw = 6e3 + 4e3 * rng.random()
                kind = "am"
            stations.append((hz, level, bw, kind))
        return stations

    def _tick(self) -> None:
        n = config.FFT_SIZE
        span = self.span_hz
        f = self.center_hz - span / 2 + (self._bins + 0.5) * span / n   # bin centre frequencies
        t = time.monotonic() - self._t0

        # Noise floor: higher at HF, scaled by the gain setting.
        floor = -105.0 + (self._gain_db - 30.0) * 0.6
        if self.center_hz < 30e6:
            floor += 15.0 * (1.0 - self.center_hz / 30e6)
        spec = floor + self._rng.normal(0.0, 2.5, n)

        # Stations.
        for hz, level, bw, kind in self._stations:
            fade = 3.0 * math.sin(t * 0.7 + hz * 1e-6)
            shape = np.exp(-0.5 * ((f - hz) / (bw / 2.4)) ** 2)
            if kind == "wfm":
                shape = np.clip(shape * 1.4, 0, 1)
            spec = np.maximum(spec, floor + (level + fade - floor) * shape)

        # Carrier under the VFO so tuning feels alive.
        vfo = self.center_hz + self._offset_hz
        vfo_level = -60.0 + 8.0 * math.sin(t * 0.4)
        spec = np.maximum(spec, floor + (vfo_level - floor) * np.exp(-0.5 * ((f - vfo) / 900.0) ** 2))

        # DC spike at the hardware centre.
        spec[n // 2 - 1:n // 2 + 2] = np.maximum(spec[n // 2 - 1:n // 2 + 2], floor + 25.0)

        # Roll-off at the band edges like the real anti-alias filter.
        edge = np.clip((np.abs(self._bins - n / 2) - n * 0.46) / (n * 0.04), 0, 1)
        spec -= 25.0 * edge

        self.spectrum_ready.emit(spec.astype(np.float32))

        # Fake S-meter: strongest thing inside the passband.
        bw = self.state.mode_spec.bandwidth_hz
        mask = np.abs(f - vfo) < bw / 2
        self.state.set_signal_db(float(spec[mask].max()) if mask.any() else floor)
