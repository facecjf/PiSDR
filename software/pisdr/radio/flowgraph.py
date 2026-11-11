"""GNU Radio backend for the RTL-SDR Blog V4.

Flowgraph::

    osmosdr.source (1.92 MS/s) --+--> FftSink -> spectrum_ready  (bigger FFT + crop when zoomed)
                                 |
                                 +--> freq_xlating_fir (decim 8 -> 240 kS/s, VFO to 0 Hz)
                                        |
                                        +--> probe_avg_mag_sqrd -> S-meter
                                        +--> copy (gated, WFM only) -> RDS chain -> PS / RadioText
                                        |
                                        +--> selector --> demods --> selector
                                                            |            |
                                       CW decode tap <------+ (CW pre-AGC output)
                                                                         |
       audio_bus <- [NR] <- [NB] <- audio filters (IIR, C++) <----------+
          |-> volume -> audio.sink                                       +--> copy (gated) ->
          |-> copy (gated) -> wavfile_sink                                     22.05 kHz -> multimon-ng

    WavPlayer (playback) replaces the filter chain as the input of audio_bus while playing.

[NB]/[NR] are Python blocks and are only wired in while their level is > 0, so the default
audio path never waits on the Python GIL (which the waterfall painter holds a lot).

Changing the bias tee or entering network mode tears the whole flowgraph down (which is
the only reliable way to make gr-osmosdr close the dongle) and builds a fresh one.

Run ``python3 -m pisdr.radio.flowgraph --freq 162.4e6 --mode NFM`` for a headless test.
"""
from __future__ import annotations

import argparse
import gc
import logging
import os
import shutil
import subprocess
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
from ..sysinfo import lan_address
from .audio_dsp import AudioFilters, NoiseReducer, PeakBlanker
from .backend import Backend
from .cw_decoder import CwEnvelopeSink
from .demod import build_all
from .audio_out import open_sink
from .fft_sink import FftSink
from .gr_extras import DecoderFeed, RdsChain, WavPlayer, decoder_resampler

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
        self._bias = state.bias_tee
        self._mode_index = {m: i for i, m in enumerate(config.MODES)}
        self._record_path: Path | None = None
        self._rtl_tcp: subprocess.Popen | None = None
        self.tb = gr.top_block("pisdr")
        self._build()
        self._poll = QTimer(self)
        self._poll.setInterval(config.SIGNAL_POLL_MS)
        self._poll.timeout.connect(self._poll_level)

    # ------------------------------------------------------------------ construction
    def _make_source(self) -> osmosdr.source:
        args = f"numchan=1 {config.RTL_DEVICE_ARGS}" + (",bias=1" if self._bias else "")
        src = osmosdr.source(args=args)
        src.set_sample_rate(config.SAMPLE_RATE)
        src.set_freq_corr(self.state.ppm, 0)
        src.set_dc_offset_mode(0, 0)
        src.set_iq_balance_mode(0, 0)
        src.set_gain_mode(False, 0)
        src.set_gain(config.DEFAULT_GAIN_DB, 0)
        src.set_bandwidth(0, 0)   # 0 = automatic
        log.info("osmosdr source opened with args %r, rate %.0f", args, src.get_sample_rate())
        return src

    def _build(self) -> None:
        tb = self.tb
        self.player: WavPlayer | None = None
        self.src = self._make_source()
        self.fft = FftSink(config.FFT_SIZE, self.state.fft_fps, self._on_fft)
        xlate_taps = firdes.low_pass(1.0, config.SAMPLE_RATE, config.IF_RATE * 0.45, config.IF_RATE * 0.1)
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(XLATE_DECIM, xlate_taps, 0.0, config.SAMPLE_RATE)
        self.probe = _probe_avg_mag_sqrd_c(0, 0.01)
        self.sel_in = blocks.selector(gr.sizeof_gr_complex, 0, 0)
        self.sel_out = blocks.selector(gr.sizeof_float, 0, 0)
        self.demods = build_all()
        self.audio_filters = AudioFilters()
        self.nb = PeakBlanker()
        self.nr = NoiseReducer()
        self._audio_extra: list = []     # Python blocks currently spliced between filters and audio_bus
        self.audio_bus = blocks.multiply_const_ff(1.0)
        self.audio_null = blocks.null_sink(gr.sizeof_float)   # takes the radio audio during playback
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
        tb.connect(self.sel_out, self.audio_filters)
        self._audio_extra = self._wanted_extra()
        chain = [self.audio_filters, *self._audio_extra, self.audio_bus]
        for a, b in zip(chain, chain[1:]):
            tb.connect(a, b)
        tb.connect(self.audio_bus, self.volume)
        tb.connect(self.volume, self.audio_sink)
        # Recordings are taken before the volume control, so their level does not depend on it.
        tb.connect(self.audio_bus, self.rec_gate)
        tb.connect(self.rec_gate, self.wav)

        # CW decoder tap (pre-AGC CW output): all heavy lifting in C++, ~1 kHz envelope into Python.
        cw_taps = firdes.low_pass(1.0, config.AUDIO_RATE, config.CW_DECODE_BW_HZ / 2, config.CW_DECODE_BW_HZ / 2)
        self.cw_xlate = gr_filter.freq_xlating_fir_filter_fcf(
            config.CW_ENVELOPE_DECIM, cw_taps, self.state.cw_pitch_hz, config.AUDIO_RATE)
        self.cw_mag = blocks.complex_to_mag(1)
        self.cw_sink = CwEnvelopeSink(self._on_cw_text)
        tb.connect((self.demods["CW"], 1), self.cw_xlate)
        tb.connect(self.cw_xlate, self.cw_mag)
        tb.connect(self.cw_mag, self.cw_sink)

        # Data decoder tap (multimon-ng). Gated off unless a decoder is selected.
        self.dec_gate = blocks.copy(gr.sizeof_float)
        self.dec_gate.set_enabled(False)
        self.dec_resamp = decoder_resampler()
        self.dec_feed = DecoderFeed(self.multimon.write)
        tb.connect(self.sel_out, self.dec_gate, self.dec_resamp, self.dec_feed)

        # RDS: optional, and must never stop the radio from starting.
        self.rds: RdsChain | None = None
        try:
            self.rds = RdsChain(tb, self.xlate, self._on_rds)
        except ImportError:
            log.info("gr-rds not installed; RDS disabled (sudo apt install gr-rds)")
        except Exception:
            log.exception("RDS chain failed to build; RDS disabled")
            self.rds = None
        self._apply_dsp(rewire=False)

    def _drop_graph(self) -> None:
        """Release every block so gr-osmosdr closes the dongle and ALSA closes the card."""
        self.tb = None
        self.src = None
        self.audio_sink = None
        self.player = None
        self.rds = None
        gc.collect()

    def _teardown(self) -> None:
        self._poll.stop()
        if self.state.recording:
            self.state.set_recording(False)
            self._hw_set_recording(False)
        if self.tb is not None:
            self.tb.stop()
            self.tb.wait()
        self._drop_graph()

    def _rebuild(self) -> str | None:
        """Fresh top block + blocks, started. Returns an error text or None."""
        last: Exception | None = None
        for _attempt in range(3):
            try:
                self.tb = gr.top_block("pisdr")
                self._build()
                break
            except Exception as exc:   # device still busy, audio card busy, ...
                last = exc
                log.warning("radio rebuild failed: %s", exc)
                self._drop_graph()
                time.sleep(1.0)
        else:
            self.radio_down = True
            return f"Radio did not reopen: {last}"
        self.radio_down = False
        self.tb.start()
        self._poll.start()
        return None

    def _on_cw_text(self, text: str) -> None:
        # Radio thread -> Qt thread via queued signal.
        self.cw_text.emit(text)

    def _on_rds(self, field: str, text: str) -> None:
        self.rds_data.emit(field, text)

    def _on_fft(self, db: np.ndarray) -> None:
        # Called from the GNU Radio scheduler thread; Qt queues it to the GUI thread.
        self.spectrum_ready.emit(db)

    def _on_play_done(self) -> None:
        self.playback_finished.emit()

    @property
    def rds_available(self) -> bool:
        return self.rds is not None

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None:
        self.tb.start()
        self._poll.start()
        self.status.emit("RTL-SDR running")

    def _hw_stop(self) -> None:
        self._poll.stop()
        self._stop_rtl_tcp()
        if self.tb is not None:
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
        self.cw_sink.set_enabled(mode == "CW")

    def _hw_set_filter(self, mode: str, width_hz: float) -> None:
        self.demods[mode].set_bandwidth(width_hz)

    def _hw_set_cw_pitch(self, hz: int) -> None:
        self.demods["CW"].set_pitch(hz)
        self.cw_xlate.set_center_freq(hz)

    def _hw_set_ppm(self, ppm: int) -> None:
        self.src.set_freq_corr(ppm, 0)

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
        """gr-osmosdr only takes the bias-tee flag at open time, so rebuild with the new flag."""
        if on == self._bias:
            return
        self._bias = on
        self.status.emit("Reopening the dongle...")
        self._teardown()
        error = self._rebuild()
        if error:
            self.status.emit(error)
            return
        self._apply_all()
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

    def _hw_set_view(self, center_hz: float, span_hz: float, zoom: int) -> None:
        size = config.FFT_SIZE * zoom
        low_edge = self.center_hz - self.span_hz / 2
        start = int(round((center_hz - span_hz / 2 - low_edge) / self.span_hz * size))
        self.fft.set_view(size, start)

    def _hw_set_spectrum_enabled(self, on: bool) -> None:
        self.fft.set_enabled(on)

    def _hw_set_dsp(self) -> None:
        self._apply_dsp()

    def _hw_set_rds(self, on: bool) -> None:
        if self.rds is not None:
            self.rds.set_enabled(on)

    def _hw_reset_rds(self) -> None:
        if self.rds is not None:
            self.rds.reset()

    def _hw_set_decoder_feed(self, on: bool) -> None:
        self.dec_gate.set_enabled(on)

    def _wanted_extra(self) -> list:
        extra = []
        if self.state.dsp_nb_level > 0:
            extra.append(self.nb)
        if self.state.dsp_nr_level > 0:
            extra.append(self.nr)
        return extra

    def _apply_dsp(self, rewire: bool = True) -> None:
        s = self.state
        self.audio_filters.apply(
            hpf=s.dsp_hpf, hpf_hz=s.dsp_hpf_hz,
            lpf=s.dsp_lpf, lpf_hz=s.dsp_lpf_hz,
            notch=s.dsp_notch, notch_hz=s.dsp_notch_hz,
        )
        self.nb.set_level(s.dsp_nb_level)
        self.nr.set_level(s.dsp_nr_level)
        if rewire:
            self._rewire_audio(self._wanted_extra())

    def _chain_target(self):
        """Where the DSP chain ends: the audio bus, or a null sink while a recording plays."""
        return self.audio_null if self.player is not None else self.audio_bus

    def _rewire_audio(self, extra: list) -> None:
        """Splice Python NB/NR blocks in or out; only restarts the graph when the set changes."""
        if [id(b) for b in extra] == [id(b) for b in self._audio_extra]:
            return
        tb = self.tb
        target = self._chain_target()
        old = [self.audio_filters, *self._audio_extra, target]
        new = [self.audio_filters, *extra, target]
        tb.lock()
        try:
            for a, b in zip(old, old[1:]):
                tb.disconnect(a, b)
            for a, b in zip(new, new[1:]):
                tb.connect(a, b)
            self._audio_extra = list(extra)
        finally:
            tb.unlock()
        names = [type(b).__name__ for b in extra] or ["none"]
        log.info("audio chain python blocks: %s", ", ".join(names))

    def _hw_play(self, path: str | None) -> str | None:
        tb = self.tb
        chain_end = [self.audio_filters, *self._audio_extra][-1]
        if path is None:
            if self.player is None:
                return None
            tb.lock()
            try:
                tb.disconnect(self.player, self.audio_bus)
                tb.disconnect(chain_end, self.audio_null)
                tb.connect(chain_end, self.audio_bus)
            finally:
                tb.unlock()
            self.player.close()
            self.player = None
            return None
        try:
            player = WavPlayer(path, self._on_play_done)
        except Exception as exc:
            return f"Cannot play: {exc}"
        tb.lock()
        try:
            if self.player is not None:
                tb.disconnect(self.player, self.audio_bus)
            else:
                tb.disconnect(chain_end, self.audio_bus)
                tb.connect(chain_end, self.audio_null)
            tb.connect(player, self.audio_bus)
        finally:
            tb.unlock()
        if self.player is not None:
            self.player.close()
        self.player = player
        return None

    # ------------------------------------------------------------------ network mode (rtl_tcp)
    def _hw_network(self, on: bool) -> str | None:
        if not on:
            self._stop_rtl_tcp()
            error = self._rebuild()
            return error
        exe = shutil.which("rtl_tcp")
        if not exe:
            return "rtl_tcp not found (sudo apt install rtl-sdr)"
        self._teardown()
        cmd = [exe, "-a", "0.0.0.0", "-p", str(config.RTL_TCP_PORT)]
        if self.state.ppm:
            cmd += ["-P", str(self.state.ppm)]
        if self.state.bias_tee and _rtl_tcp_has_bias_flag(exe):
            cmd += ["-T"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as exc:
            return f"rtl_tcp failed: {exc}" + _suffix(self._rebuild())
        time.sleep(0.8)
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
            reason = err[-1] if err else f"code {proc.returncode}"
            return f"rtl_tcp exited: {reason}" + _suffix(self._rebuild())
        self._rtl_tcp = proc
        log.info("rtl_tcp serving on %s:%d", lan_address(), config.RTL_TCP_PORT)
        return None

    def _stop_rtl_tcp(self) -> None:
        proc, self._rtl_tcp = self._rtl_tcp, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        except OSError as exc:
            log.warning("could not stop rtl_tcp: %s", exc)

    @property
    def rtl_tcp_alive(self) -> bool:
        return self._rtl_tcp is not None and self._rtl_tcp.poll() is None

    # ------------------------------------------------------------------ S-meter
    def _poll_level(self) -> None:
        level = self.probe.level()
        db = 10.0 * np.log10(max(level, 1e-12)) + self.state.smeter_cal_db
        self.state.set_signal_db(float(db))
        if self.cw_sink.enabled:
            self.state.set_cw_wpm(self.cw_sink.decoder.wpm)


def _suffix(error: str | None) -> str:
    return f"; {error}" if error else ""


_BIAS_FLAG: dict[str, bool] = {}


def _rtl_tcp_has_bias_flag(exe: str) -> bool:
    """Older rtl_tcp builds have no -T (bias tee) option and exit if given one."""
    if exe not in _BIAS_FLAG:
        try:
            out = subprocess.run([exe, "-h"], capture_output=True, text=True, timeout=3)
            _BIAS_FLAG[exe] = "-T" in (out.stdout + out.stderr)
        except (OSError, subprocess.TimeoutExpired):
            _BIAS_FLAG[exe] = False
    return _BIAS_FLAG[exe]


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
    backend.rds_data.connect(lambda f, t: print(f"RDS {f}: {t}"))
    backend.start()
    print(f"Listening on {args.freq / 1e6:.4f} MHz {args.mode} for {args.seconds:.0f} s ...")
    QtCore.QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec()
    backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
