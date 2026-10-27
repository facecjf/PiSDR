"""GNU Radio backend for the RTL-SDR Blog V4.

Flowgraph::

    osmosdr.source (1.92 MS/s) --+--> FftSink -> spectrum_ready
                                 |
                                 +--> freq_xlating_fir (decim 8 -> 240 kS/s, VFO to 0 Hz)
                                        |
                                        +--> probe_avg_mag_sqrd -> S-meter
                                        |
                                        +--> selector --> AM / NFM / WFM / USB / LSB / CW --> selector
                                                                                                  |
                                                                 volume -> audio.sink <-----------+
                                                                        -> copy (gated) -> wavfile_sink

Run ``python3 -m pisdr.radio.flowgraph --freq 162.4e6 --mode NFM`` for a headless test.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from gnuradio import blocks, filter as gr_filter, gr
from gnuradio.filter import firdes
import osmosdr

from .. import config
from ..qt import QTimer
from .backend import Backend
from .demod import build_all
from .audio_out import open_sink
from .fft_sink import FftSink

# Moved from gnuradio.blocks to gnuradio.analog in GNU Radio 3.8+.
try:
    from gnuradio import analog as _analog
    _probe_avg_mag_sqrd_c = _analog.probe_avg_mag_sqrd_c
except AttributeError:  # pragma: no cover - pre-3.8 fallback
    _probe_avg_mag_sqrd_c = blocks.probe_avg_mag_sqrd_c

log = logging.getLogger(__name__)

XLATE_DECIM = config.SAMPLE_RATE // config.IF_RATE   # 8


class GnuRadioBackend(Backend):
    def __init__(self, state, parent=None) -> None:
        super().__init__(state, parent)
        self.tb = gr.top_block("pisdr")
        self._bias = state.bias_tee
        self._mode_index = {m: i for i, m in enumerate(config.MODES)}
        self._build()
        self._poll = QTimer(self)
        self._poll.setInterval(config.SIGNAL_POLL_MS)
        self._poll.timeout.connect(self._poll_level)
        self._record_path: Path | None = None

    # ------------------------------------------------------------------ construction
    def _make_source(self) -> osmosdr.source:
        args = f"numchan=1 {config.RTL_DEVICE_ARGS}" + (",bias=1" if self._bias else "")
        src = osmosdr.source(args=args)
        src.set_sample_rate(config.SAMPLE_RATE)
        src.set_freq_corr(config.PPM_CORRECTION, 0)
        src.set_dc_offset_mode(0, 0)
        src.set_iq_balance_mode(0, 0)
        src.set_gain_mode(False, 0)
        src.set_gain(config.DEFAULT_GAIN_DB, 0)
        src.set_bandwidth(0, 0)   # 0 = automatic
        log.info("osmosdr source opened with args %r, rate %.0f", args, src.get_sample_rate())
        return src

    def _build(self) -> None:
        tb = self.tb
        self.src = self._make_source()
        self.fft = FftSink(config.FFT_SIZE, self.state.fft_fps, self._on_fft)
        xlate_taps = firdes.low_pass(1.0, config.SAMPLE_RATE, config.IF_RATE * 0.45, config.IF_RATE * 0.1)
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(XLATE_DECIM, xlate_taps, 0.0, config.SAMPLE_RATE)
        self.probe = _probe_avg_mag_sqrd_c(0, 0.01)
        self.sel_in = blocks.selector(gr.sizeof_gr_complex, 0, 0)
        self.sel_out = blocks.selector(gr.sizeof_float, 0, 0)
        self.demods = build_all()
        self.volume = blocks.multiply_const_ff(0.5)
        self.audio_sink, self.audio_device = open_sink(config.AUDIO_RATE)
        self.rec_gate = blocks.copy(gr.sizeof_float)
        self.rec_gate.set_enabled(False)
        dummy = os.path.join(tempfile.gettempdir(), "pisdr_dummy.wav")
        self.wav = blocks.wavfile_sink(dummy, 1, config.AUDIO_RATE, blocks.FORMAT_WAV, blocks.FORMAT_PCM_16, False)
        self.wav.close()
        try:
            os.remove(dummy)
        except OSError:
            pass

        tb.connect(self.src, self.fft)
        tb.connect(self.src, self.xlate)
        tb.connect(self.xlate, self.probe)
        tb.connect(self.xlate, self.sel_in)
        for mode, index in self._mode_index.items():
            tb.connect((self.sel_in, index), self.demods[mode])
            tb.connect(self.demods[mode], (self.sel_out, index))
        tb.connect(self.sel_out, self.volume)
        tb.connect(self.volume, self.audio_sink)
        tb.connect(self.volume, self.rec_gate)
        tb.connect(self.rec_gate, self.wav)

    def _on_fft(self, db: np.ndarray) -> None:
        # Called from the GNU Radio scheduler thread; Qt queues it to the GUI thread.
        self.spectrum_ready.emit(db)

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None:
        self.tb.start()
        self._poll.start()
        self.status.emit("RTL-SDR running")

    def _hw_stop(self) -> None:
        self._poll.stop()
        self.tb.stop()
        self.tb.wait()

    def _hw_set_center(self, hz: float) -> None:
        self.src.set_center_freq(hz, 0)

    def _hw_set_offset(self, hz: float) -> None:
        # Translate the VFO frequency to 0 Hz; the demodulators handle their own sideband offsets.
        self.xlate.set_center_freq(hz)

    def _hw_set_mode(self, mode: str) -> None:
        index = self._mode_index[mode]
        self.sel_in.set_output_index(index)
        self.sel_out.set_input_index(index)
        self._hw_set_squelch(self.state.squelch_db)

    def _hw_set_volume(self, pct: int) -> None:
        self.volume.set_k((pct / 100.0) ** 2)

    def _hw_set_squelch(self, db: float) -> None:
        for demod in self.demods.values():
            if hasattr(demod, "set_squelch"):
                demod.set_squelch(db)

    def _hw_set_gain(self, db: float, agc: bool) -> None:
        self.src.set_gain_mode(bool(agc), 0)
        if not agc:
            self.src.set_gain(db, 0)

    def _hw_set_bias_tee(self, on: bool) -> None:
        """gr-osmosdr only takes the bias-tee flag at open time, so swap the source."""
        if on == self._bias:
            return
        self._bias = on
        self.tb.lock()
        try:
            self.tb.disconnect(self.src, self.fft)
            self.tb.disconnect(self.src, self.xlate)
            self.src = self._make_source()
            self.src.set_center_freq(self.center_hz, 0)
            self._hw_set_gain(self.state.gain_db, self.state.agc)
            self.tb.connect(self.src, self.fft)
            self.tb.connect(self.src, self.xlate)
        finally:
            self.tb.unlock()
        self.status.emit("Bias tee ON" if on else "Bias tee OFF")

    def _hw_set_recording(self, on: bool) -> None:
        if on:
            config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = config.RECORDINGS_DIR / f"pisdr_{stamp}_{self.state.vfo_hz}_{self.state.mode}.wav"
            self.wav.open(str(path))
            self.rec_gate.set_enabled(True)
            self._record_path = path
            self.status.emit(f"Recording {path.name}")
        else:
            self.rec_gate.set_enabled(False)
            self.wav.close()
            if self._record_path:
                self.status.emit(f"Saved {self._record_path.name}")
            self._record_path = None

    def _hw_set_fft_fps(self, fps: int) -> None:
        self.fft.set_fps(fps)

    # ------------------------------------------------------------------ S-meter
    def _poll_level(self) -> None:
        level = self.probe.level()
        db = 10.0 * np.log10(max(level, 1e-12)) + config.SMETER_CAL_DB
        self.state.set_signal_db(float(db))


# --------------------------------------------------------------------------- CLI test
def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiSDR headless receiver test")
    parser.add_argument("--freq", type=float, default=162.4e6, help="VFO frequency in Hz")
    parser.add_argument("--mode", default="NFM", choices=config.MODES)
    parser.add_argument("--gain", type=float, default=config.DEFAULT_GAIN_DB)
    parser.add_argument("--squelch", type=float, default=-100.0)
    parser.add_argument("--volume", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from ..qt import QtCore
    from ..state import RadioState

    app = QtCore.QCoreApplication(sys.argv[:1])
    state = RadioState()
    state.set_mode(args.mode)
    state.set_gain(args.gain)
    state.set_squelch(args.squelch)
    state.set_volume(args.volume)
    state.set_vfo(args.freq)
    backend = GnuRadioBackend(state)

    frames = {"n": 0}

    def on_spectrum(db):
        frames["n"] += 1
        if frames["n"] % 25 == 0:
            print(f"spectrum frame {frames['n']}: floor {np.percentile(db, 30):.1f} dB, "
                  f"peak {db.max():.1f} dB, S-meter {state.signal_db:.0f}")

    backend.spectrum_ready.connect(on_spectrum)
    backend.status.connect(lambda s: print("status:", s))
    backend.start()
    print(f"Listening on {args.freq / 1e6:.4f} MHz {args.mode} for {args.seconds:.0f} s ...")
    QtCore.QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec()
    backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
