"""Power spectra for the display, taken from the sample stream by GNU Radio's own blocks.

::

    samples -> selector -+-> stream_to_vector(N)   -> keep_one_in_n -> fft_vcc -> |x|^2 -> probe    x1
                         +-> stream_to_vector(2N)  -> keep_one_in_n -> fft_vcc -> |x|^2 -> probe    x2
                         +-> ...                                                                    x64

There is one chain per zoom factor, because a block's vector length cannot change while
the graph runs. The selector sends the samples down the chain in use (or nowhere, while
the spectrum is switched off); the others are idle. ``keep_one_in_n`` thins the vectors
out to about twice the display rate before the FFT, and the probe at the end just keeps
the newest result. The GUI thread fetches it with :meth:`SpectrumTap.read` on a timer.

Nothing here runs Python in the radio's threads, and nothing here can make the dongle
wait. That is the point: the block this replaces was a Python sink on the 1.92 MS/s
stream, which had to take the interpreter lock for every chunk of samples. Whenever the
GUI thread was busy (a fast waterfall, the aircraft map with long trails) the sink got
the lock too rarely, the source backed up behind it, and the audio broke up. Measured on
GNU Radio 3.10 with the main thread busy in Python: 61 % of the samples got through with
that sink, 82 % with its FFT switched off, 100 % with this.

Zoom: the chain for zoom z transforms z * N samples, and :meth:`read` returns the N bins
of the window being shown, so a zoomed view has real extra resolution.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from gnuradio import blocks, fft, gr

_FLOOR = 1e-20        # -200 dB: keeps log10 off zero


@dataclass
class _Chain:
    port: int             # of the selector
    size: int             # FFT length
    keep: object          # keep_one_in_n
    probe: object         # probe_signal_vf
    gain: float           # what a full-scale carrier comes to, so that it reads 0 dB
    seen: list | None = None     # the last vector read from the probe


class SpectrumTap:
    """*out_bins* dB values per spectrum, at up to *fps* a second, at any of *zooms*."""

    def __init__(self, tb, source, out_bins: int, sample_rate: float, zooms: tuple[int, ...], fps: int) -> None:
        self.out_bins = out_bins
        self.enabled = True
        self._rate = float(sample_rate)
        self._skip = 0
        self.selector = blocks.selector(gr.sizeof_gr_complex, 0, 0)
        tb.connect(source, self.selector)
        self._chains: dict[int, _Chain] = {}
        for port, zoom in enumerate(zooms):
            size = out_bins * zoom
            window = np.blackman(size).astype(np.float32)
            to_vector = blocks.stream_to_vector(gr.sizeof_gr_complex, size)
            keep = blocks.keep_one_in_n(gr.sizeof_gr_complex * size, 1)
            transform = fft.fft_vcc(size, True, window.tolist(), True, 1)      # forward, DC in the middle
            power = blocks.complex_to_mag_squared(size)
            probe = blocks.probe_signal_vf(size)
            tb.connect((self.selector, port), to_vector, keep, transform, power, probe)
            self._chains[size] = _Chain(port, size, keep, probe, float(np.sum(window)) ** 2)
        self._chain = self._chains[out_bins * zooms[0]]
        self._start = 0
        self.set_fps(fps)
        self._restart()

    @property
    def size(self) -> int:
        """Length of the FFT in use."""
        return self._chain.size

    @property
    def probe(self):
        """The block holding the newest power spectrum of the chain in use."""
        return self._chain.probe

    def set_fps(self, fps: int) -> None:
        """Vectors are transformed at about twice this rate, so a read finds a fresh one."""
        for chain in self._chains.values():
            vectors_per_s = self._rate / chain.size
            chain.keep.set_n(max(1, int(vectors_per_s / (2 * max(1, fps)))))

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        self.selector.set_enabled(on)          # off: the samples are dropped before any chain
        if on and not self.enabled:
            self._restart()
        self.enabled = on

    def set_view(self, size: int, start_bin: int) -> None:
        """FFT of *size* points, show bins [start_bin, start_bin + out_bins)."""
        chain = self._chains.get(size, self._chain)
        self._start = int(max(0, min(chain.size - self.out_bins, start_bin)))
        if chain is not self._chain:
            self._chain = chain
            self.selector.set_output_index(chain.port)
            self._restart()

    def _restart(self) -> None:
        """What the probe holds is from before, and the next vector is partly from before."""
        self._chain.seen = self._chain.probe.level()
        self._skip = 1

    def read(self) -> np.ndarray | None:
        """The newest spectrum in dB (ascending frequency), or None if there is no new one.

        "New" is "differs from the last one read": the probe has no counter, and the noise
        in a receiver never gives the same spectrum twice.
        """
        if not self.enabled:
            return None
        chain = self._chain
        level = chain.probe.level()
        if level == chain.seen:
            return None
        chain.seen = level
        if self._skip:
            self._skip -= 1
            return None
        power = np.asarray(level[self._start:self._start + self.out_bins], dtype=np.float32)
        return (10.0 * np.log10(np.maximum(power / chain.gain, _FLOOR))).astype(np.float32)
