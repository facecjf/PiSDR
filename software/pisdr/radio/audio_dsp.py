"""Post-demod audio DSP for the GNU Radio backend.

Default audio chain is pure C++ (cheap IIR biquads) so it never touches the Python GIL::

    demod -> AudioFilters (HPF -> LPF -> notch) -> volume

Noise blanker and noise reduction are Python ``sync_block``s. Python blocks in the audio
path compete with Qt painting for the GIL, so the flowgraph only splices them in when
their level is > 0 (see ``GnuRadioBackend._rewire_audio``).
"""
from __future__ import annotations

import logging
import math

import numpy as np
from gnuradio import filter as gr_filter, gr

from .. import config

log = logging.getLogger(__name__)
AUDIO_RATE = config.AUDIO_RATE

# 4th-order Butterworth as two cascaded biquads.
_BUTTER4_Q = (0.5412, 1.3066)


def _pass_iir() -> tuple[list[float], list[float]]:
    return [1.0], [1.0]


def _biquad(kind: str, f_hz: float, q: float) -> tuple[list[float], list[float]]:
    """RBJ cookbook biquad, scipy-style (b, a) with a[0] == 1."""
    f = max(10.0, min(AUDIO_RATE * 0.45, float(f_hz)))
    w0 = 2.0 * math.pi * f / AUDIO_RATE
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * max(q, 0.05))
    if kind == "lpf":
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = b0
    elif kind == "hpf":
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = b0
    elif kind == "notch":
        b0 = 1.0
        b1 = -2.0 * cos_w0
        b2 = 1.0
    else:
        raise ValueError(kind)
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def _iir() -> gr_filter.iir_filter_ffd:
    ff, fb = _pass_iir()
    # oldstyle=False: feedback taps use the scipy sign convention.
    return gr_filter.iir_filter_ffd(ff, fb, False)


class AudioFilters(gr.hier_block2):
    """HPF (2 biquads) -> LPF (2 biquads) -> notch (1 biquad). C++ only."""

    def __init__(self) -> None:
        gr.hier_block2.__init__(
            self, "audio_filters",
            gr.io_signature(1, 1, gr.sizeof_float),
            gr.io_signature(1, 1, gr.sizeof_float),
        )
        self.hpf = (_iir(), _iir())
        self.lpf = (_iir(), _iir())
        self.notch = _iir()
        self.connect(self, self.hpf[0], self.hpf[1], self.lpf[0], self.lpf[1], self.notch, self)

    def apply(self, *, hpf: bool, hpf_hz: int, lpf: bool, lpf_hz: int,
              notch: bool, notch_hz: int) -> None:
        for blk, q in zip(self.hpf, _BUTTER4_Q):
            ff, fb = _biquad("hpf", hpf_hz, q) if hpf else _pass_iir()
            blk.set_taps(ff, fb)
        for blk, q in zip(self.lpf, _BUTTER4_Q):
            ff, fb = _biquad("lpf", lpf_hz, q) if lpf else _pass_iir()
            blk.set_taps(ff, fb)
        if notch:
            q = float(notch_hz) / max(20.0, config.DSP_NOTCH_WIDTH_HZ)
            ff, fb = _biquad("notch", notch_hz, q)
        else:
            ff, fb = _pass_iir()
        self.notch.set_taps(ff, fb)


class PeakBlanker(gr.sync_block):
    """Audio-rate impulse blanker (Python). Only wired in when level > 0."""

    def __init__(self) -> None:
        gr.sync_block.__init__(
            self, name="peak_blanker",
            in_sig=[np.float32], out_sig=[np.float32],
        )
        self.level = 5
        self._avg = 1e-4

    def set_level(self, level: int) -> None:
        self.level = int(max(0, min(config.DSP_LEVEL_MAX, level)))

    def work(self, input_items, output_items):
        x = input_items[0]
        y = output_items[0]
        n = len(y)
        if self.level <= 0:
            y[:] = x[:n]
            return n
        mag = np.abs(x[:n])
        mean_mag = float(np.mean(mag)) if n else 0.0
        self._avg = 0.98 * self._avg + 0.02 * max(mean_mag, 1e-9)
        thresh = (14.0 - 0.9 * self.level) * self._avg
        np.copyto(y[:n], x[:n])
        y[:n][mag > thresh] = 0.0
        return n


class NoiseReducer(gr.sync_block):
    """Spectral-subtraction NR (Python). Only wired in when level > 0."""

    def __init__(self, fft_size: int = config.DSP_NR_FFT) -> None:
        gr.sync_block.__init__(
            self, name="noise_reducer",
            in_sig=[np.float32], out_sig=[np.float32],
        )
        self.fft_size = int(fft_size)
        self.window = np.hanning(self.fft_size).astype(np.float32)
        self.noise = np.full(self.fft_size // 2 + 1, 1e-4, dtype=np.float32)
        self.level = 5
        self._noise_alpha = 0.05
        self._env = 1e-4

    def set_level(self, level: int) -> None:
        self.level = int(max(0, min(config.DSP_LEVEL_MAX, level)))

    def work(self, input_items, output_items):
        x = input_items[0]
        y = output_items[0]
        n = len(y)
        if self.level <= 0:
            y[:] = x[:n]
            return n

        strength = 0.2 + 0.07 * self.level
        floor = max(0.08, 0.4 - 0.03 * self.level)
        pos = 0
        while pos + self.fft_size <= n:
            frame = x[pos: pos + self.fft_size] * self.window
            spec = np.fft.rfft(frame)
            mag = np.abs(spec).astype(np.float32)
            quiet = mag < (self.noise * 1.8)
            self.noise[quiet] = (
                (1.0 - self._noise_alpha) * self.noise[quiet]
                + self._noise_alpha * mag[quiet]
            )
            np.maximum(self.noise, 1e-6, out=self.noise)
            gain = 1.0 - strength * (self.noise / np.maximum(mag, 1e-6))
            np.clip(gain, floor, 1.0, out=gain)
            cleaned = np.fft.irfft(spec * gain, n=self.fft_size).astype(np.float32)
            y[pos: pos + self.fft_size] = cleaned * self.window
            pos += self.fft_size

        if pos < n:
            rest = x[pos:n]
            mag = np.abs(rest)
            self._env = 0.95 * self._env + 0.05 * float(np.mean(mag) if len(mag) else 0.0)
            gate = np.clip(mag / max(self._env * (2.5 - 0.1 * self.level), 1e-6), floor, 1.0)
            y[pos:n] = rest * gate.astype(np.float32)
        return n
