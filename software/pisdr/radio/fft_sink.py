"""GNU Radio sink block that computes a power spectrum a few times per second.

The block consumes everything it is given (so it never back-pressures the flowgraph) and,
at most ``fps`` times per second, takes the most recent ``fft_size`` samples, applies a
Blackman window and hands the dB spectrum (fft-shifted, ascending frequency) to a
callback. The callback runs in the GNU Radio scheduler thread; the backend forwards it to
the Qt thread through a queued signal.

Zoom: :meth:`set_view` raises the FFT size by the zoom factor and returns only
``out_bins`` bins starting at ``start_bin``, so a zoomed view has real extra resolution.
"""
from __future__ import annotations

import time
from typing import Callable, NamedTuple

import numpy as np
from gnuradio import gr


class _View(NamedTuple):
    size: int
    start: int
    count: int
    window: np.ndarray
    gain: float


def _make_view(size: int, start: int, count: int) -> _View:
    window = np.blackman(size).astype(np.float32)
    start = int(max(0, min(size - count, start)))
    return _View(size, start, count, window, float(np.sum(window)))


class FftSink(gr.sync_block):
    def __init__(self, fft_size: int, fps: int, callback: Callable[[np.ndarray], None]) -> None:
        gr.sync_block.__init__(self, name="pisdr_fft_sink",
                               in_sig=[np.complex64], out_sig=None)
        self.out_bins = fft_size
        self.callback = callback
        self.enabled = True
        self.set_fps(fps)
        # Replaced as a whole (one attribute store) so the radio thread never sees a mix.
        self._view = _make_view(fft_size, 0, fft_size)
        self._next_t = 0.0
        self._buf = np.zeros(fft_size, dtype=np.complex64)
        self._fill = 0

    def set_fps(self, fps: int) -> None:
        self._interval = 1.0 / max(1, fps)

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            self._fill = 0

    def set_view(self, size: int, start_bin: int) -> None:
        """FFT of *size* points, keep bins [start_bin, start_bin + out_bins)."""
        self._view = _make_view(max(size, self.out_bins), start_bin, self.out_bins)

    def work(self, input_items, output_items) -> int:
        samples = input_items[0]
        n = len(samples)
        if not self.enabled:
            return n
        view = self._view
        size = view.size
        if len(self._buf) != size:
            self._buf = np.zeros(size, dtype=np.complex64)
            self._fill = 0
        now = time.monotonic()
        if now >= self._next_t:
            if n >= size:
                self._compute(samples[-size:], view)
                self._next_t = now + self._interval
                self._fill = 0
            else:
                # Accumulate small chunks until we have a full frame.
                take = min(n, size - self._fill)
                self._buf[self._fill:self._fill + take] = samples[:take]
                self._fill += take
                if self._fill == size:
                    self._compute(self._buf, view)
                    self._next_t = now + self._interval
                    self._fill = 0
        return n

    def _compute(self, frame: np.ndarray, view: _View) -> None:
        spectrum = np.fft.fftshift(np.fft.fft(frame * view.window))
        spectrum = spectrum[view.start:view.start + view.count]
        power = (np.abs(spectrum) / view.gain) ** 2
        db = 10.0 * np.log10(np.maximum(power, 1e-20)).astype(np.float32)
        self.callback(db)
