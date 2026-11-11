"""Demodulator hierarchical blocks.

Every demodulator takes complex baseband at ``IF_RATE`` (240 kS/s, VFO already translated
to 0 Hz by the flowgraph's xlating filter) and produces float audio at ``AUDIO_RATE``
(48 kS/s). They are all built once and sit behind a pair of ``blocks.selector`` so that only
the active branch receives samples and burns CPU.

Each demodulator has ``set_bandwidth(hz)``; CW also has ``set_pitch(hz)``. Both just swap
FIR taps, which GNU Radio applies safely while the flowgraph runs.
"""
from __future__ import annotations

import math

from gnuradio import analog, blocks, filter as gr_filter, gr
from gnuradio.filter import firdes

from .. import config

IF_RATE = config.IF_RATE
AUDIO_RATE = config.AUDIO_RATE
DECIM = IF_RATE // AUDIO_RATE   # 5


def _channel_taps(cutoff_hz: float, transition_hz: float):
    return firdes.low_pass(1.0, IF_RATE, cutoff_hz, transition_hz)


def _channel_lpf(cutoff_hz: float, transition_hz: float):
    """Complex low-pass + decimate from IF_RATE to AUDIO_RATE."""
    return gr_filter.fir_filter_ccf(DECIM, _channel_taps(cutoff_hz, transition_hz))


class AmDemod(gr.hier_block2):
    def __init__(self, bandwidth_hz: float = 8_000) -> None:
        gr.hier_block2.__init__(self, "am_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        self.chan = _channel_lpf(bandwidth_hz / 2, 1_500)
        agc = analog.agc2_cc(attack_rate=0.1, decay_rate=0.002, reference=0.3, gain=1.0)
        mag = blocks.complex_to_mag(1)
        dc = gr_filter.dc_blocker_ff(1024, True)
        self.audio_lpf = gr_filter.fir_filter_fff(1, self._audio_taps(bandwidth_hz))
        self.connect(self, self.chan, agc, mag, dc, self.audio_lpf, self)

    @staticmethod
    def _audio_taps(bandwidth_hz: float):
        return firdes.low_pass(1.0, AUDIO_RATE, min(bandwidth_hz / 2 + 500, 6_000), 1_000)

    def set_bandwidth(self, bandwidth_hz: float) -> None:
        self.chan.set_taps(_channel_taps(bandwidth_hz / 2, 1_500))
        self.audio_lpf.set_taps(self._audio_taps(bandwidth_hz))


class NfmDemod(gr.hier_block2):
    """Narrow FM with power squelch. ``set_squelch(db)``; -100 opens it fully."""

    def __init__(self, bandwidth_hz: float = 12_500, max_dev_hz: float = 5_000) -> None:
        gr.hier_block2.__init__(self, "nfm_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        self.chan = _channel_lpf(bandwidth_hz / 2, 2_000)
        self.squelch = analog.pwr_squelch_cc(-100.0, 1e-3, 0, True)
        demod = analog.nbfm_rx(audio_rate=AUDIO_RATE, quad_rate=AUDIO_RATE, tau=75e-6, max_dev=max_dev_hz)
        self.connect(self, self.chan, self.squelch, demod, self)

    def set_squelch(self, db: float) -> None:
        self.squelch.set_threshold(db)

    def set_bandwidth(self, bandwidth_hz: float) -> None:
        self.chan.set_taps(_channel_taps(bandwidth_hz / 2, 2_000))


class WfmDemod(gr.hier_block2):
    """Broadcast FM (mono) with power squelch."""

    def __init__(self) -> None:
        gr.hier_block2.__init__(self, "wfm_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        chan = gr_filter.fir_filter_ccf(1, firdes.low_pass(1.0, IF_RATE, 90_000, 20_000))
        self.squelch = analog.pwr_squelch_cc(-100.0, 1e-3, 0, True)
        demod = analog.wfm_rcv(quad_rate=IF_RATE, audio_decimation=DECIM)
        self.connect(self, chan, self.squelch, demod, self)

    def set_squelch(self, db: float) -> None:
        self.squelch.set_threshold(db)

    def set_bandwidth(self, bandwidth_hz: float) -> None:
        """Broadcast FM has a single fixed width."""


class SsbDemod(gr.hier_block2):
    """USB or LSB. The VFO is the (suppressed) carrier; select one sideband and take the real part."""

    def __init__(self, upper: bool, low_cut_hz: float = config.SSB_LOW_CUT_HZ,
                 high_cut_hz: float = config.SSB_HIGH_CUT_HZ) -> None:
        name = "usb_demod" if upper else "lsb_demod"
        gr.hier_block2.__init__(self, name,
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        self.upper = upper
        self.low_cut_hz = low_cut_hz
        chan = _channel_lpf(6_000, 2_000)
        self.bpf = gr_filter.fir_filter_ccc(1, self._taps(high_cut_hz - low_cut_hz))
        agc = analog.agc2_cc(attack_rate=0.5, decay_rate=0.01, reference=0.25, gain=1.0)
        real = blocks.complex_to_real(1)
        self.connect(self, chan, self.bpf, agc, real, self)

    def _taps(self, bandwidth_hz: float):
        lo, hi = self.low_cut_hz, self.low_cut_hz + bandwidth_hz
        if not self.upper:
            lo, hi = -hi, -lo
        return firdes.complex_band_pass(1.0, AUDIO_RATE, lo, hi, 200)

    def set_bandwidth(self, bandwidth_hz: float) -> None:
        self.bpf.set_taps(self._taps(bandwidth_hz))


class CwDemod(gr.hier_block2):
    """CW: shift the carrier to an audible pitch, narrow band-pass, real part.

    Output 0 is the audio (after AGC). Output 1 is the same tone before the AGC, for the
    Morse decoder: the AGC raises the noise between elements and would hide the keying.
    """

    def __init__(self, pitch_hz: float = config.CW_PITCH_HZ, bandwidth_hz: float = 500) -> None:
        gr.hier_block2.__init__(self, "cw_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(2, 2, gr.sizeof_float))
        self.pitch_hz = pitch_hz
        self.bandwidth_hz = bandwidth_hz
        chan = _channel_lpf(3_000, 1_500)
        self.rot = blocks.rotator_cc(self._phase_inc())
        self.bpf = gr_filter.fir_filter_ccc(1, self._taps())
        agc = analog.agc2_cc(attack_rate=0.5, decay_rate=0.01, reference=0.25, gain=1.0)
        real = blocks.complex_to_real(1)
        self.connect(self, chan, self.rot, self.bpf, agc, real, self)
        self.connect(self.bpf, blocks.complex_to_real(1), (self, 1))

    def _phase_inc(self) -> float:
        return 2.0 * math.pi * self.pitch_hz / AUDIO_RATE

    def _taps(self):
        half = self.bandwidth_hz / 2
        return firdes.complex_band_pass(1.0, AUDIO_RATE, self.pitch_hz - half, self.pitch_hz + half,
                                        min(100, half))

    def set_bandwidth(self, bandwidth_hz: float) -> None:
        self.bandwidth_hz = bandwidth_hz
        self.bpf.set_taps(self._taps())

    def set_pitch(self, pitch_hz: float) -> None:
        self.pitch_hz = pitch_hz
        self.rot.set_phase_inc(self._phase_inc())
        self.bpf.set_taps(self._taps())


def build_all() -> dict[str, gr.hier_block2]:
    """One instance of every demodulator, keyed by the mode names in config.MODES."""
    return {
        "AM": AmDemod(config.MODE_SPECS["AM"].bandwidth_hz),
        "NFM": NfmDemod(config.MODE_SPECS["NFM"].bandwidth_hz),
        "WFM": WfmDemod(),
        "USB": SsbDemod(upper=True),
        "LSB": SsbDemod(upper=False),
        "CW": CwDemod(bandwidth_hz=config.MODE_SPECS["CW"].bandwidth_hz),
    }
