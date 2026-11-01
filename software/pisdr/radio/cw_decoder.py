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


class MorseDecoder:
    """Envelope → text. Call :meth:`process` with float envelope chunks."""

    def __init__(self, on_text: Callable[[str], None], rate: float = ENVELOPE_RATE,
                 wpm_lock: float | None = None) -> None:
        self.on_text = on_text
        self.rate = float(rate)
        self.wpm_lock = wpm_lock
        # Adaptive levels.
        self._lo = 0.0
        self._hi = 1e-3
        self._nstd = 1e-3                   # noise standard deviation (key-up samples)
        self._thresh = 1e-3
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
        env = np.asarray(env, dtype=np.float32)
        block_min = float(env.min())
        block_max = float(env.max())
        if not self._init:
            self._lo, self._hi = block_min, block_max
            self._nstd = max(float(env.std()), 1e-6)
            self._init = True
        # Floor: fast when falling, slow when rising. Peak: fast rise, slow fall.
        self._lo += (block_min - self._lo) * (0.3 if block_min < self._lo else 0.02)
        self._hi += (block_max - self._hi) * (0.3 if block_max > self._hi else 0.01)
        if self._hi < self._lo + 1e-6:
            self._hi = self._lo + 1e-6
        # Noise spread from the key-up samples of this block.
        quiet = env[env < self._thresh]
        if len(quiet) >= 8:
            self._nstd += (max(float(quiet.std()), 1e-6) - self._nstd) * 0.2
        # Threshold: midpoint, but never inside the noise.
        mid = self._lo + (self._hi - self._lo) * 0.5
        thresh = max(mid, self._lo + 4.0 * self._nstd)
        self._thresh = thresh
        hyst = max((self._hi - self._lo) * 0.08, self._nstd * 0.5)

        for v in env:
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
