"""GNU Radio sink block that computes a power spectrum a few times per second.

The block consumes everything it is given (so it never back-pressures the flowgraph) and,
at most ``fps`` times per second, takes the most recent ``fft_size`` samples, applies a
Blackman-Harris window and hands the dB spectrum (fft-shifted, ascending frequency) to a
callback. The callback runs in the GNU Radio scheduler thread; the backend forwards it to
the Qt thread through a queued signal.
"""
from __future__ import annotations

import time
from typing import Callable

import numpy as np
from gnuradio import gr


class FftSink(gr.sync_block):
    def __init__(self, fft_size: int, fps: int, callback: Callable[[np.ndarray], None]) -> None:
        gr.sync_block.__init__(self, name="pisdr_fft_sink",
                               in_sig=[np.complex64], out_sig=None)
        self.fft_size = fft_size
        self.callback = callback
        self.enabled = True
        self.set_fps(fps)
        self._window = np.blackman(fft_size).astype(np.float32)
        self._window_gain = float(np.sum(self._window))
        self._next_t = 0.0
        self._buf = np.zeros(fft_size, dtype=np.complex64)
        self._fill = 0

    def set_fps(self, fps: int) -> None:
        self._interval = 1.0 / max(1, fps)

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            self._fill = 0

    def work(self, input_items, output_items) -> int:
        samples = input_items[0]
        n = len(samples)
        if not self.enabled:
            return n
        now = time.monotonic()
        if now >= self._next_t:
            if n >= self.fft_size:
                self._compute(samples[-self.fft_size:])
                self._next_t = now + self._interval
                self._fill = 0
            else:
                # Accumulate small chunks until we have a full frame.
                take = min(n, self.fft_size - self._fill)
                self._buf[self._fill:self._fill + take] = samples[:take]
                self._fill += take
                if self._fill == self.fft_size:
                    self._compute(self._buf)
                    self._next_t = now + self._interval
                    self._fill = 0
        return n

    def _compute(self, frame: np.ndarray) -> None:
        spectrum = np.fft.fftshift(np.fft.fft(frame * self._window))
        power = (np.abs(spectrum) / self._window_gain) ** 2
        db = 10.0 * np.log10(np.maximum(power, 1e-20)).astype(np.float32)
        self.callback(db)
