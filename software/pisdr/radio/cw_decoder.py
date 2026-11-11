"""Morse (CW) decoder.

:class:`MorseDecoder` is pure Python: feed it an envelope sampled at ``ENVELOPE_RATE``
(≈1 kHz) and it emits characters through a callback. It tracks the noise/tone levels
adaptively, estimates the dit length from the mark durations it sees (so WPM follows
the sender), and classifies marks and gaps against that unit.

:class:`CwEnvelopeSink` is the thin GNU Radio wrapper. Everything expensive (band-pass,
magnitude, low-pass, decimation) is done upstream in C++ blocks; this sink only sees the
~1 kHz envelope, so even if the GIL is busy painting the waterfall the audio path is
unaffected and the decoder merely runs a little late.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

import numpy as np

from .. import config

log = logging.getLogger(__name__)

ENVELOPE_RATE = config.CW_ENVELOPE_RATE

MORSE_TABLE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G",
    "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N",
    "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T", "..-": "U",
    "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5",
    "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'", "-.-.--": "!", "-..-.": "/",
    "-.--.": "(", "-.--.-": ")", ".-...": "&", "---...": ":", "-.-.-.": ";", "-...-": "=",
    ".-.-.": "+", "-....-": "-", "..--.-": "_", ".-..-.": '"', "...-..-": "$", ".--.-.": "@",
    "...-.-": "<SK>", ".-.-": "<AA>", "-...-.-": "<BK>", "........": "<ERR>",
}


def _smoothing(rate: float, time_constant_s: float) -> float:
    """Per-sample weight of a one-pole smoother with the given time constant."""
    return 1.0 - math.exp(-1.0 / (rate * time_constant_s))


class MorseDecoder:
    """Envelope → text. Call :meth:`process` with float envelope chunks."""

    NOISE_MARGIN = 3.0      # key-down needs the envelope this many noise std-devs above the floor

    def __init__(self, on_text: Callable[[str], None], rate: float = ENVELOPE_RATE,
                 wpm_lock: float | None = None) -> None:
        self.on_text = on_text
        self.rate = float(rate)
        self.wpm_lock = wpm_lock
        # Adaptive levels, updated per sample so they follow time, not GNU Radio chunk sizes.
        self._k_fast = _smoothing(self.rate, 0.010)    # peak attack
        self._k_track = _smoothing(self.rate, 0.050)   # mark level while key-down, noise while key-up
        self._k_slow = _smoothing(self.rate, 1.5)      # mark level fading out during long key-up
        self._settle = int(0.015 * self.rate)          # envelope filter ring-down after a mark
        self._lo = 0.0
        self._hi = 0.0
        self._nvar = 0.0                    # noise variance around the floor (key-up samples)
        self._init = False
        self._key_down = False
        self._run = 0                       # samples in current mark/space state
        self._symbols = ""
        self._dit_s = 1.2 / 20.0            # initial guess: 20 WPM
        self._mark_hist: list[float] = []
        self._word_flushed = True
        self.chars_decoded = 0

    # ------------------------------------------------------------------ public
    @property
    def wpm(self) -> float:
        return 1.2 / self._dit_s if self._dit_s > 0 else 0.0

    @property
    def dit_s(self) -> float:
        return self._dit_s

    def set_wpm_lock(self, wpm: float | None) -> None:
        self.wpm_lock = wpm
        if wpm:
            self._dit_s = 1.2 / wpm

    def reset(self) -> None:
        self._symbols = ""
        self._run = 0
        self._key_down = False
        self._word_flushed = True
        self._init = False

    def process(self, env: np.ndarray) -> None:
        if len(env) == 0:
            return
        env = np.asarray(env, dtype=np.float64)
        if not self._init:
            self._lo = self._hi = float(np.median(env))
            self._nvar = float(env.var()) or (self._lo * 0.5) ** 2
            self._init = True
        kf, kt, ks = self._k_fast, self._k_track, self._k_slow
        lo, hi, nvar = self._lo, self._hi, self._nvar
        settle = self._settle
        thresh = max(lo + (hi - lo) * 0.5, lo + self.NOISE_MARGIN * nvar ** 0.5)

        for v in env:
            if v > hi:
                hi += (v - hi) * kf
            if self._key_down:
                hi += (v - hi) * kt
            else:
                hi += (lo - hi) * ks
                # Noise statistics only from settled key-up samples, not the falling edge of a mark.
                if self._run > settle and v < thresh:
                    nvar += ((v - lo) ** 2 - nvar) * kt
                    lo += (v - lo) * kt
            if v < lo:
                lo += (v - lo) * kf
            # Threshold: midpoint between floor and mark level, but never inside the noise.
            nstd = nvar ** 0.5
            thresh = max(lo + (hi - lo) * 0.5, lo + self.NOISE_MARGIN * nstd)
            hyst = max((hi - lo) * 0.08, nstd * 0.5)
            if self._key_down:
                down = v > thresh - hyst
            else:
                down = v > thresh + hyst
            if down == self._key_down:
                self._run += 1
                if not down:
                    self._check_gap()
                continue
            # State changed.
            dur = self._run / self.rate
            if self._key_down:
                self._on_mark(dur)
            else:
                self._on_space(dur)
            self._key_down = down
            self._run = 1
        self._lo, self._hi, self._nvar = lo, hi, nvar

    # ------------------------------------------------------------------ internals
    def _on_mark(self, dur: float) -> None:
        if dur < self._dit_s * 0.3:
            return  # glitch
        if self.wpm_lock is None:
            self._update_speed(dur)
        self._symbols += "." if dur < self._dit_s * 2.0 else "-"
        self._word_flushed = False

    def _on_space(self, dur: float) -> None:
        # Space before a mark: letter/word gaps are also handled live in _check_gap.
        pass

    def _check_gap(self) -> None:
        """Called while key-up: flush letter after 2.5 dits, word after 6 dits."""
        gap = self._run / self.rate
        if self._symbols and gap > self._dit_s * 2.5:
            self._emit_symbols()
        if not self._word_flushed and gap > self._dit_s * 6.0:
            self._word_flushed = True
            self.on_text(" ")

    def _emit_symbols(self) -> None:
        text = MORSE_TABLE.get(self._symbols, "*")
        self._symbols = ""
        self.chars_decoded += 1
        self.on_text(text)

    def _update_speed(self, mark_dur: float) -> None:
        """Estimate dit length from recent mark durations (two clusters: dits and dahs)."""
        self._mark_hist.append(mark_dur)
        if len(self._mark_hist) > 24:
            self._mark_hist.pop(0)
        if len(self._mark_hist) < 3:
            return
        arr = np.array(self._mark_hist)
        lo, hi = float(arr.min()), float(arr.max())
        if hi < lo * 1.8:
            # Only one cluster seen so far: treat it as dits if it is near our estimate,
            # otherwise as dahs.
            cluster = float(np.median(arr))
            est = cluster if cluster < self._dit_s * 2.0 else cluster / 3.0
        else:
            split = (lo + hi) / 2.0
            dits = arr[arr < split]
            dahs = arr[arr >= split]
            est_d = float(np.median(dits)) if len(dits) else None
            est_h = float(np.median(dahs)) / 3.0 if len(dahs) else None
            if est_d is not None and est_h is not None:
                est = (est_d + est_h) / 2.0
            else:
                est = est_d if est_d is not None else est_h
        est = max(1.2 / 60.0, min(1.2 / 5.0, est))   # clamp 5..60 WPM
        self._dit_s += (est - self._dit_s) * 0.3


try:  # GNU Radio is only present on the Pi.
    from gnuradio import gr

    class CwEnvelopeSink(gr.sync_block):
        """Sink for the ~1 kHz CW envelope; runs :class:`MorseDecoder`."""

        def __init__(self, on_text: Callable[[str], None]) -> None:
            gr.sync_block.__init__(self, name="cw_envelope_sink",
                                   in_sig=[np.float32], out_sig=None)
            self.decoder = MorseDecoder(on_text)
            self.enabled = False

        def set_enabled(self, on: bool) -> None:
            if on and not self.enabled:
                self.decoder.reset()
            self.enabled = bool(on)

        def work(self, input_items, output_items) -> int:
            n = len(input_items[0])
            if self.enabled:
                self.decoder.process(input_items[0])
            return n

except ImportError:  # pragma: no cover - development PC
    CwEnvelopeSink = None  # type: ignore[assignment]
