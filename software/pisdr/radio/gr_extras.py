"""Small GNU Radio blocks for playback, data decoders and RDS.

* :class:`WavPlayer` - Python source that plays a 16-bit WAV into the audio chain.
* :class:`DecoderFeed` - Python sink that hands 22.05 kHz s16 audio to multimon-ng.
* :func:`build_rds` - gr-rds receive chain (FM multiplex -> 57 kHz BPSK -> PS / RadioText),
  adapted from gr-rds' ``rds_rx`` example to PiSDR's 240 kS/s IF. Optional: needs the
  ``gr-rds`` package; :func:`build_rds` raises ImportError without it.
"""
from __future__ import annotations

import logging
import math
import wave
from typing import Callable

import numpy as np
from gnuradio import analog, blocks, filter as gr_filter, gr
from gnuradio.filter import firdes

from .. import config

log = logging.getLogger(__name__)


class WavPlayer(gr.sync_block):
    """Plays *path* once, then outputs silence and calls *on_done* (from the radio thread)."""

    def __init__(self, path: str, on_done: Callable[[], None]) -> None:
        gr.sync_block.__init__(self, name="pisdr_wav_player", in_sig=None, out_sig=[np.float32])
        self._wav: wave.Wave_read | None = wave.open(path, "rb")
        if self._wav.getsampwidth() != 2:
            self._wav.close()
            raise ValueError("only 16-bit WAV files are supported")
        self._channels = self._wav.getnchannels()
        self._on_done = on_done

    def work(self, input_items, output_items) -> int:
        out = output_items[0]
        n = len(out)
        wav = self._wav
        if wav is None:
            out[:] = 0.0
            return n
        data = np.frombuffer(wav.readframes(n), dtype="<i2")
        if self._channels > 1:
            data = data[:: self._channels]
        m = len(data)
        out[:m] = data.astype(np.float32) / 32768.0
        if m < n:
            out[m:] = 0.0
            wav.close()
            self._wav = None
            self._on_done()
        return n

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class DecoderFeed(gr.sync_block):
    """Float audio in, signed 16-bit little-endian bytes out to ``write`` (non-blocking)."""

    SCALE = 20_000.0

    def __init__(self, write: Callable[[bytes], None]) -> None:
        gr.sync_block.__init__(self, name="pisdr_decoder_feed", in_sig=[np.float32], out_sig=None)
        self._write = write

    def work(self, input_items, output_items) -> int:
        samples = input_items[0]
        pcm = np.clip(samples * self.SCALE, -32767.0, 32767.0).astype("<i2")
        self._write(pcm.tobytes())
        return len(samples)


def decoder_resampler():
    """48 kHz -> 22.05 kHz (x147 / 320)."""
    assert config.AUDIO_RATE * 147 // 320 == config.DECODER_RATE
    return gr_filter.rational_resampler_fff(interpolation=147, decimation=320)


class RdsSink(gr.basic_block):
    """Receives gr-rds parser messages ``(type, text)`` and forwards PS / RT / PTY."""

    FIELDS = {1: "ps", 2: "pty", 4: "rt"}

    def __init__(self, callback: Callable[[str, str], None]) -> None:
        import pmt
        gr.basic_block.__init__(self, name="pisdr_rds_sink", in_sig=None, out_sig=None)
        self._pmt = pmt
        self._callback = callback
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self._handle)

    def _handle(self, msg) -> None:
        pmt = self._pmt
        try:
            kind = pmt.to_long(pmt.tuple_ref(msg, 0))
            text = pmt.symbol_to_string(pmt.tuple_ref(msg, 1))
        except Exception:   # malformed message; never let it reach the scheduler
            return
        field = self.FIELDS.get(kind)
        if field:
            self._callback(field, " ".join(text.replace("\x00", " ").split()))


class RdsChain:
    """Blocks of the RDS receiver. ``gate`` is a ``blocks.copy`` that is off unless in WFM."""

    def __init__(self, tb: gr.top_block, source, callback: Callable[[str, str], None]) -> None:
        import rds   # gr-rds; ImportError when the package is missing
        from gnuradio import digital

        rate = config.IF_RATE
        self.gate = blocks.copy(gr.sizeof_gr_complex)
        self.gate.set_enabled(False)
        fm = analog.quadrature_demod_cf(rate / (2 * math.pi * 75_000))
        # 57 kHz subcarrier to baseband, decimate 10 -> 24 kS/s, then 24 k -> 19 k (16 x 1187.5 Hz).
        sub = gr_filter.freq_xlating_fir_filter_fcc(
            10, firdes.low_pass(1.0, rate, 7_500, 5_000), 57_000, rate)
        resamp = gr_filter.rational_resampler_ccc(interpolation=19, decimation=24)
        rrc = firdes.root_raised_cosine(1.0, 19_000, 19_000 / 8, 1.0, 151)
        manchester = [rrc[n] - rrc[n + 8] for n in range(len(rrc) - 8)]
        matched = gr_filter.fir_filter_ccc(1, manchester)
        agc = analog.agc_cc(2e-3, 0.585, 53)
        agc.set_max_gain(1000)
        bpsk = digital.constellation_bpsk().base()
        sync = digital.symbol_sync_cc(digital.TED_ZERO_CROSSING, 16, 0.01, 1.0, 1.0, 0.1, 1,
                                      bpsk, digital.IR_MMSE_8TAP, 128, [])
        receiver = digital.constellation_receiver_cb(bpsk, 2 * math.pi / 100, -0.002, 0.002)
        try:
            diff = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        except (AttributeError, TypeError):
            diff = digital.diff_decoder_bb(2)
        self.decoder = rds.decoder(False, False)
        self.parser = rds.parser(False, False, 0)
        self.sink = RdsSink(callback)

        tb.connect(source, self.gate, fm, sub, resamp, matched, agc, sync)
        tb.connect(sync, (receiver, 0))
        tb.connect((receiver, 0), diff, self.decoder)
        tb.msg_connect((self.decoder, "out"), (self.parser, "in"))
        tb.msg_connect((self.parser, "out"), (self.sink, "in"))

    def set_enabled(self, on: bool) -> None:
        self.gate.set_enabled(bool(on))

    def reset(self) -> None:
        try:
            self.parser.reset()
        except Exception:
            pass
