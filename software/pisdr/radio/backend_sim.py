"""Simulated receiver for developing the UI without any hardware.

Produces a plausible spectrum: a noise floor that rises toward HF, a handful of
deterministic "stations" scattered across every 1.92 MHz block so there is always
something to tune to, a DC spike where the dongle would have one, and a slowly fading
carrier wherever the VFO happens to be so the S-meter moves. No audio is generated.

RDS, data decoders, playback, network mode and ADS-B traffic are faked just enough to
exercise the UI.
"""
from __future__ import annotations

import math
import time
import wave

import numpy as np

from .. import config
from ..adsb import SimTraffic, receiver_position
from ..qt import QTimer
from .backend import Backend
from .cw_decoder import MORSE_TABLE, MorseDecoder

_SIM_CW_TEXT = "CQ CQ CQ DE PISDR PISDR K "
_SIM_CW_WPM = 18

_SIM_DECODER_LINES = {
    "APRS": ("AFSK1200: fm N9PIS-9 to APWW11 via WIDE1-1,WIDE2-1 UI  pid=F0",
             "!4325.12N/08811.40W>146.520MHz PiSDR simulated beacon"),
    "POCSAG": ("POCSAG1200: Address: 1234567  Function: 3  Alpha:   SIMULATED PAGE",),
    "FLEX": ("FLEX|1600/2/K/A|07.077|001234567|ALN|SIMULATED FLEX MESSAGE",),
    "EAS": ("EAS: ZCZC-WXR-RWT-055131+0030-3071405-KMKX/NWS-",),
    "DTMF": ("DTMF: 1", "DTMF: 2", "DTMF: #"),
}


def _sim_cw_pattern(text: str, wpm: int, rate: int) -> np.ndarray:
    """On/off envelope for *text* at *wpm*, sampled at *rate* Hz."""
    rev = {v: k for k, v in MORSE_TABLE.items() if len(v) == 1}
    dit = max(1, int(round(rate * 1.2 / wpm)))
    out: list[int] = []
    for word in text.split(" "):
        for ch in word:
            for sym in rev.get(ch, ""):
                out += [1] * (dit if sym == "." else 3 * dit)
                out += [0] * dit
            out += [0] * 2 * dit
        out += [0] * 4 * dit
    return np.array(out or [0], dtype=np.float32)


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
        self._zoom = 1
        self._t0 = time.monotonic()
        self._stations: list[tuple[float, float, float, str]] = []  # (hz, level_db, bw_hz, kind)
        self._cw_pattern = _sim_cw_pattern(_SIM_CW_TEXT, _SIM_CW_WPM, config.CW_ENVELOPE_RATE)
        self._cw_pos = 0
        self._cw_last_t = time.monotonic()
        self._cw_decoder = MorseDecoder(self.cw_text.emit)

        self._rds_on = False
        self._rds_timer = QTimer(self)
        self._rds_timer.setSingleShot(True)
        self._rds_timer.timeout.connect(self._emit_rds)
        self._dec_name = "OFF"
        self._dec_i = 0
        self._dec_timer = QTimer(self)
        self._dec_timer.timeout.connect(self._emit_decoder_line)
        self._play_timer = QTimer(self)
        self._play_timer.setSingleShot(True)
        self._play_timer.timeout.connect(self.playback_finished.emit)
        self._traffic: SimTraffic | None = None

    @property
    def rds_available(self) -> bool:
        return True

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None:
        self._timer.start(int(1000 / self.state.fft_fps))
        if self.reason and self.reason != "--sim requested":
            self.status.emit(f"SIMULATOR - {self.reason[:70]} (run: python3 -m pisdr --check)")
        else:
            self.status.emit("SIMULATOR - no radio hardware")

    def _hw_stop(self) -> None:
        self._timer.stop()
        self._dec_timer.stop()
        self._play_timer.stop()

    def _hw_set_center(self, hz: float) -> None:
        self._stations = self._make_stations(hz)

    def _hw_set_offset(self, hz: float) -> None:
        self._offset_hz = hz

    def _hw_set_mode(self, mode: str) -> None:
        if mode == "CW" and self._mode != "CW":
            self._cw_decoder.reset()
            self._cw_last_t = time.monotonic()
        self._mode = mode

    def _hw_set_gain(self, db: float, agc: bool) -> None:
        self._gain_db = 35.0 if agc else db

    def _hw_set_fft_fps(self, fps: int) -> None:
        if self._timer.isActive():
            self._timer.start(int(1000 / fps))

    def _hw_set_view(self, center_hz: float, span_hz: float, zoom: int) -> None:
        self._zoom = zoom

    def _hw_set_bias_tee(self, on: bool) -> None:
        self.status.emit("Bias tee ON (simulated)" if on else "Bias tee OFF (simulated)")

    def _hw_set_recording(self, on: bool) -> None:
        self.status.emit("Recording (simulated)" if on else "Recording stopped")

    def _hw_set_rds(self, on: bool) -> None:
        self._rds_on = on
        if on:
            self._rds_timer.start(1500)
        else:
            self._rds_timer.stop()

    def _hw_reset_rds(self) -> None:
        if self._rds_on:
            self._rds_timer.start(1500)

    def _emit_rds(self) -> None:
        if not self._rds_on:
            return
        mhz = self.state.vfo_hz / 1e6
        self.rds_data.emit("ps", f"SIM{mhz:5.1f}".replace(" ", ""))
        self.rds_data.emit("pty", "Pop Music")
        self.rds_data.emit("rt", f"PiSDR simulated RadioText on {mhz:.1f} MHz")

    def _set_decoder(self, name: str) -> None:
        self.state.clear_decoder_lines()
        self._dec_name = name
        self._dec_i = 0
        if name == "OFF":
            self._dec_timer.stop()
            return
        self._dec_timer.start(2500)
        self.status.emit(f"{name} decoder on (simulated)")

    def _emit_decoder_line(self) -> None:
        lines = _SIM_DECODER_LINES.get(self._dec_name)
        if not lines or self.state.dongle_lent:
            return
        self.state.append_decoder_line(lines[self._dec_i % len(lines)])
        self._dec_i += 1

    def _hw_play(self, path: str | None) -> str | None:
        if path is None:
            self._play_timer.stop()
            return None
        try:
            with wave.open(path, "rb") as wav:
                seconds = wav.getnframes() / max(1, wav.getframerate())
        except (OSError, EOFError, wave.Error) as exc:
            return f"Cannot play: {exc}"
        self._play_timer.start(int(max(1.0, seconds) * 1000))
        self.status.emit(f"Playing {seconds:.0f} s (simulated, no audio)")
        return None

    def _hw_network(self, on: bool) -> str | None:
        if on:
            self._timer.stop()
        else:
            self._timer.start(int(1000 / self.state.fft_fps))
        return None

    def _hw_adsb(self, on: bool) -> str | None:
        if on:
            self._timer.stop()
            where = receiver_position(self.state) or config.ADSB_SIM_HOME
            self._traffic = SimTraffic(where[0], where[1])
        else:
            self._traffic = None
            self._timer.start(int(1000 / self.state.fft_fps))
        return None

    def _hw_adsb_poll(self) -> tuple[list, int, str]:
        if self._traffic is None:
            return [], 0, ""
        aircraft, messages = self._traffic.aircraft()
        return aircraft, messages, ""

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
        span = self.view_span_hz
        bin_hz = span / n
        f = self.view_center_hz - span / 2 + (self._bins + 0.5) * bin_hz   # bin centre frequencies
        t = time.monotonic() - self._t0

        # Noise floor: higher at HF, scaled by the gain setting, lower per bin when zoomed.
        floor = -105.0 + (self._gain_db - 30.0) * 0.6 - 10.0 * math.log10(self._zoom)
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

        # DC spike at the hardware centre (only visible if the view includes it).
        dc = np.abs(f - self.center_hz) < 1.5 * bin_hz
        spec[dc] = np.maximum(spec[dc], floor + 25.0)

        # Roll-off at the hardware band edges like the real anti-alias filter.
        rel = np.abs(f - self.center_hz) / self.span_hz
        spec -= 25.0 * np.clip((rel - 0.46) / 0.04, 0, 1)

        self.spectrum_ready.emit(spec.astype(np.float32))

        # Fake S-meter: strongest thing inside the passband.
        mode_spec = self.state.mode_spec
        mask = np.abs(f - (vfo + mode_spec.offset_hz)) < max(mode_spec.bandwidth_hz / 2, bin_hz)
        level = float(spec[mask].max()) if mask.any() else floor
        self.state.set_signal_db(level + self.state.smeter_cal_db - config.SMETER_CAL_DB)

        if self._mode == "CW":
            self._tick_cw()

    def _tick_cw(self) -> None:
        """Feed the decoder a slice of a looping simulated CW envelope."""
        now = time.monotonic()
        n = int((now - self._cw_last_t) * config.CW_ENVELOPE_RATE)
        if n <= 0:
            return
        self._cw_last_t = now
        idx = (self._cw_pos + np.arange(n)) % len(self._cw_pattern)
        self._cw_pos = int((self._cw_pos + n) % len(self._cw_pattern))
        env = self._cw_pattern[idx] * 0.8 + 0.05 + self._rng.normal(0.0, 0.03, n).astype(np.float32)
        self._cw_decoder.process(np.abs(env))
        self.state.set_cw_wpm(self._cw_decoder.wpm)
