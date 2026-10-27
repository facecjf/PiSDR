"""Demodulator hierarchical blocks.

Every demodulator takes complex baseband at ``IF_RATE`` (240 kS/s, VFO already translated
to 0 Hz by the flowgraph's xlating filter) and produces float audio at ``AUDIO_RATE``
(48 kS/s). They are all built once and sit behind a pair of ``blocks.selector`` so that only
the active branch receives samples and burns CPU.
"""
from __future__ import annotations

import math

from gnuradio import analog, blocks, filter as gr_filter, gr
from gnuradio.filter import firdes

from .. import config

IF_RATE = config.IF_RATE
AUDIO_RATE = config.AUDIO_RATE
DECIM = IF_RATE // AUDIO_RATE   # 5


def _channel_lpf(cutoff_hz: float, transition_hz: float):
    """Complex low-pass + decimate from IF_RATE to AUDIO_RATE."""
    taps = firdes.low_pass(1.0, IF_RATE, cutoff_hz, transition_hz)
    return gr_filter.fir_filter_ccf(DECIM, taps)


class AmDemod(gr.hier_block2):
    def __init__(self, bandwidth_hz: float = 8_000) -> None:
        gr.hier_block2.__init__(self, "am_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        chan = _channel_lpf(bandwidth_hz / 2, 1_500)
        agc = analog.agc2_cc(attack_rate=0.1, decay_rate=0.002, reference=0.3, gain=1.0)
        mag = blocks.complex_to_mag(1)
        dc = gr_filter.dc_blocker_ff(1024, True)
        audio_lpf = gr_filter.fir_filter_fff(1, firdes.low_pass(1.0, AUDIO_RATE, 4_500, 1_000))
        self.connect(self, chan, agc, mag, dc, audio_lpf, self)


class NfmDemod(gr.hier_block2):
    """Narrow FM with power squelch. ``set_squelch(db)``; -100 opens it fully."""

    def __init__(self, bandwidth_hz: float = 12_500, max_dev_hz: float = 5_000) -> None:
        gr.hier_block2.__init__(self, "nfm_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        chan = _channel_lpf(bandwidth_hz / 2, 2_000)
        self.squelch = analog.pwr_squelch_cc(-100.0, 1e-3, 0, True)
        demod = analog.nbfm_rx(audio_rate=AUDIO_RATE, quad_rate=AUDIO_RATE, tau=75e-6, max_dev=max_dev_hz)
        self.connect(self, chan, self.squelch, demod, self)

    def set_squelch(self, db: float) -> None:
        self.squelch.set_threshold(db)


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


class SsbDemod(gr.hier_block2):
    """USB or LSB. The VFO is the (suppressed) carrier; select one sideband and take the real part."""

    def __init__(self, upper: bool, low_cut_hz: float = config.SSB_LOW_CUT_HZ,
                 high_cut_hz: float = config.SSB_HIGH_CUT_HZ) -> None:
        name = "usb_demod" if upper else "lsb_demod"
        gr.hier_block2.__init__(self, name,
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        chan = _channel_lpf(6_000, 2_000)
        if upper:
            taps = firdes.complex_band_pass(1.0, AUDIO_RATE, low_cut_hz, high_cut_hz, 200)
        else:
            taps = firdes.complex_band_pass(1.0, AUDIO_RATE, -high_cut_hz, -low_cut_hz, 200)
        bpf = gr_filter.fir_filter_ccc(1, taps)
        agc = analog.agc2_cc(attack_rate=0.5, decay_rate=0.01, reference=0.25, gain=1.0)
        real = blocks.complex_to_real(1)
        self.connect(self, chan, bpf, agc, real, self)


class CwDemod(gr.hier_block2):
    """CW: shift the carrier to an audible pitch, narrow band-pass, real part."""

    def __init__(self, pitch_hz: float = config.CW_PITCH_HZ, bandwidth_hz: float = 500) -> None:
        gr.hier_block2.__init__(self, "cw_demod",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(1, 1, gr.sizeof_float))
        chan = _channel_lpf(3_000, 1_500)
        rot = blocks.rotator_cc(2.0 * math.pi * pitch_hz / AUDIO_RATE)
        taps = firdes.complex_band_pass(1.0, AUDIO_RATE, pitch_hz - bandwidth_hz / 2,
                                        pitch_hz + bandwidth_hz / 2, 100)
        bpf = gr_filter.fir_filter_ccc(1, taps)
        agc = analog.agc2_cc(attack_rate=0.5, decay_rate=0.01, reference=0.25, gain=1.0)
        real = blocks.complex_to_real(1)
        self.connect(self, chan, rot, bpf, agc, real, self)


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
