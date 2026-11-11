"""Backend interface shared by the GNU Radio flowgraph and the simulator.

A backend owns the hardware centre frequency. The UI only ever talks in terms of the VFO
frequency; the backend decides when to retune the dongle so the VFO stays inside the
visible span and away from the DC spike. :attr:`center_changed` announces the *displayed*
window, which is the full span at zoom x1 or a slice around the VFO when zoomed in.
"""
from __future__ import annotations

import logging
import wave

import numpy as np

from .. import config
from ..qt import QObject, Signal
from ..state import RadioState
from .decoders import Multimon

log = logging.getLogger(__name__)


class Backend(QObject):
    """Abstract receiver. Subclasses implement the ``_hw_*`` hooks."""

    spectrum_ready = Signal(object)        # np.ndarray of dB values, length FFT_SIZE, ascending freq
    center_changed = Signal(float, float)  # (view_center_hz, view_span_hz)
    status = Signal(str)
    cw_text = Signal(str)                  # decoded Morse characters (emitted from the radio thread)
    rds_data = Signal(str, str)            # ("ps" | "rt" | "pty", text), from the radio thread
    playback_finished = Signal()

    #: fraction of the span the VFO may wander from centre before the hardware retunes
    RETUNE_FRACTION = 0.42
    #: where the VFO is parked relative to centre after a retune (keeps it off the DC spike)
    PARK_FRACTION = 0.25
    #: when zoomed, recentre the view once the VFO is this far (fraction of view span) off centre
    VIEW_RECENTER_FRACTION = 0.4

    def __init__(self, state: RadioState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.span_hz = float(config.SAMPLE_RATE)
        self.center_hz: float = 0.0
        self.view_center_hz: float = 0.0
        self.view_span_hz: float = self.span_hz
        self.running = False
        self.radio_down = False   # set when the dongle could not be reopened
        self._spectrum_on = True
        self.multimon = Multimon(self)
        self.multimon.line.connect(state.append_decoder_line)
        self.rds_data.connect(state.set_rds)
        self.playback_finished.connect(self.stop_playback)
        state.changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.running = True
        self._hw_start()
        self._apply_all()

    def stop(self) -> None:
        self.running = False
        if self.state.recording:
            self._hw_set_recording(False)
        self.multimon.stop()
        self._hw_stop()

    def _apply_all(self) -> None:
        if self.radio_down:
            return
        s = self.state
        self._retune_for_vfo(force=True)
        self._hw_set_ppm(s.ppm)
        for mode in config.MODES:
            widths = config.FILTER_WIDTHS[mode]
            self._hw_set_filter(mode, widths[s.filter_index.get(mode, 0) % len(widths)])
        self._hw_set_cw_pitch(s.cw_pitch_hz)
        self._hw_set_mode(s.mode)
        self._hw_set_offset(self.vfo_offset_hz())
        self._hw_set_volume(s.volume)
        self._hw_set_squelch(s.squelch_db)
        self._hw_set_gain(s.gain_db, s.agc)
        self._hw_set_fft_fps(s.fft_fps)
        self._hw_set_dsp()
        self._hw_set_spectrum_enabled(self._spectrum_on)
        self._update_rds()
        self._set_decoder(s.decoder)

    _DSP_FIELDS = (
        "dsp_hpf", "dsp_hpf_hz", "dsp_lpf", "dsp_lpf_hz",
        "dsp_notch", "dsp_notch_hz", "dsp_nr_level", "dsp_nb_level",
    )

    # ------------------------------------------------------------------ state glue
    def _on_state_changed(self, field: str) -> None:
        if not self.running or self.radio_down:
            return
        s = self.state
        if field == "vfo_hz":
            self._retune_for_vfo()
            if s.mode == "WFM":
                s.clear_rds()
                self._hw_reset_rds()
        elif field == "mode":
            self._hw_set_mode(s.mode)
            self._hw_set_offset(self.vfo_offset_hz())
            self._update_rds()
        elif field == "filter_index":
            self._hw_set_filter(s.mode, s.filter_hz)
            self._hw_set_offset(self.vfo_offset_hz())
        elif field == "volume":
            self._hw_set_volume(s.volume)
        elif field == "squelch_db":
            self._hw_set_squelch(s.squelch_db)
        elif field in ("gain_db", "agc"):
            self._hw_set_gain(s.gain_db, s.agc)
        elif field == "bias_tee":
            self._hw_set_bias_tee(s.bias_tee)
        elif field == "recording":
            self._hw_set_recording(s.recording)
        elif field == "fft_fps":
            self._hw_set_fft_fps(s.fft_fps)
        elif field in self._DSP_FIELDS:
            self._hw_set_dsp()
        elif field == "ppm":
            self._hw_set_ppm(s.ppm)
        elif field == "cw_pitch_hz":
            self._hw_set_cw_pitch(s.cw_pitch_hz)
        elif field == "spectrum_zoom":
            self._update_view(force=True)
        elif field == "rds_enabled":
            self._update_rds()
        elif field == "decoder":
            self._set_decoder(s.decoder)

    def vfo_offset_hz(self) -> float:
        return self.state.vfo_hz - self.center_hz

    def _retune_for_vfo(self, force: bool = False) -> None:
        vfo = float(self.state.vfo_hz)
        retuned = False
        if force or abs(vfo - self.center_hz) > self.span_hz * self.RETUNE_FRACTION:
            park = self.span_hz * self.PARK_FRACTION
            # Park the VFO a quarter span above centre, unless that would put the lower
            # edge of the span below 0 Hz, in which case park it below centre instead.
            center = vfo - park
            if center < self.span_hz / 2:
                center = vfo + park
            self.center_hz = center
            self._hw_set_center(center)
            retuned = True
        self._hw_set_offset(self.vfo_offset_hz())
        self._update_view(force=retuned)

    def _update_view(self, force: bool = False) -> None:
        """Pick the displayed window inside the hardware span and tell the UI if it moved."""
        zoom = self.state.spectrum_zoom if self.state.spectrum_zoom in config.SPECTRUM_ZOOMS else 1
        span = self.span_hz / zoom
        vfo = float(self.state.vfo_hz)
        center = self.view_center_hz
        if force or span != self.view_span_hz or abs(vfo - center) > span * self.VIEW_RECENTER_FRACTION:
            center = vfo
        lo = self.center_hz - self.span_hz / 2 + span / 2
        hi = self.center_hz + self.span_hz / 2 - span / 2
        center = min(max(center, lo), hi)
        if force or center != self.view_center_hz or span != self.view_span_hz:
            self.view_center_hz = center
            self.view_span_hz = span
            self._hw_set_view(center, span, zoom)
            self.center_changed.emit(center, span)

    def _update_rds(self) -> None:
        on = self.state.rds_enabled and self.state.mode == "WFM"
        self._hw_set_rds(on)
        if not on:
            self.state.clear_rds()

    def _set_decoder(self, name: str) -> None:
        """Start or stop multimon-ng and gate the audio feed into it."""
        self._hw_set_decoder_feed(False)
        self.multimon.stop()
        self.state.clear_decoder_lines()
        if name == "OFF":
            return
        error = self.multimon.start(name)
        if error:
            self.status.emit(error)
            self.state.set_decoder("OFF")
            return
        self._hw_set_decoder_feed(True)
        hint = "" if name == "DTMF" or self.state.mode == "NFM" else " - most data signals need NFM"
        self.status.emit(f"{name} decoder on{hint}")

    # ------------------------------------------------------------------ playback
    def play_file(self, path: str) -> str | None:
        """Play a recording through the audio chain. Returns an error text or None."""
        if self.state.network_mode:
            return "Leave network mode first"
        try:
            with wave.open(str(path), "rb") as wav:
                if wav.getsampwidth() != 2 or wav.getframerate() != config.AUDIO_RATE:
                    return f"Only 16-bit {config.AUDIO_RATE} Hz WAV files can be played"
        except (OSError, EOFError, wave.Error) as exc:
            return f"Cannot open {path}: {exc}"
        if self.state.recording:
            self.state.set_recording(False)
        error = self._hw_play(str(path))
        if error:
            return error
        self.state.set_playing(str(path))
        return None

    def stop_playback(self) -> None:
        if not self.state.playing:
            return
        self._hw_play(None)
        self.state.set_playing("")

    # ------------------------------------------------------------------ network mode
    def start_network_mode(self) -> str | None:
        """Release the dongle to rtl_tcp. Returns an error text or None."""
        if self.state.network_mode:
            return None
        self.stop_playback()
        if self.state.recording:
            self.state.set_recording(False)
        self.multimon.stop()
        self.running = False
        error = self._hw_network(True)
        if error:
            self.running = True
            self._apply_all()
            return error
        self.state.set_network_mode(True)
        return None

    def stop_network_mode(self) -> str | None:
        if not self.state.network_mode:
            return None
        error = self._hw_network(False)
        self.state.set_network_mode(False)
        self.running = True
        self._apply_all()
        if self.radio_down:
            return (error or "Radio did not reopen") + " - restart PiSDR"
        return error

    # ------------------------------------------------------------------ hooks
    def _hw_start(self) -> None: ...
    def _hw_stop(self) -> None: ...
    def _hw_set_center(self, hz: float) -> None: ...
    def _hw_set_offset(self, hz: float) -> None: ...
    def _hw_set_mode(self, mode: str) -> None: ...
    def _hw_set_filter(self, mode: str, width_hz: float) -> None: ...
    def _hw_set_volume(self, pct: int) -> None: ...
    def _hw_set_squelch(self, db: float) -> None: ...
    def _hw_set_gain(self, db: float, agc: bool) -> None: ...
    def _hw_set_ppm(self, ppm: int) -> None: ...
    def _hw_set_cw_pitch(self, hz: int) -> None: ...
    def _hw_set_bias_tee(self, on: bool) -> None: ...
    def _hw_set_recording(self, on: bool) -> None: ...
    def _hw_set_fft_fps(self, fps: int) -> None: ...
    def _hw_set_view(self, center_hz: float, span_hz: float, zoom: int) -> None: ...
    def _hw_set_dsp(self) -> None: ...
    def _hw_set_rds(self, on: bool) -> None: ...
    def _hw_reset_rds(self) -> None: ...
    def _hw_set_decoder_feed(self, on: bool) -> None: ...
    def _hw_play(self, path: str | None) -> str | None: ...
    def _hw_network(self, on: bool) -> str | None: ...

    def set_spectrum_enabled(self, on: bool) -> None:
        """Pause FFT→UI work while the waterfall is not visible (saves audio CPU)."""
        self._spectrum_on = bool(on)
        if self.running:
            self._hw_set_spectrum_enabled(on)

    def _hw_set_spectrum_enabled(self, on: bool) -> None: ...

    @property
    def rds_available(self) -> bool:
        return False

    # ------------------------------------------------------------------ utilities
    @staticmethod
    def power_db(spectrum: np.ndarray) -> np.ndarray:
        return 10.0 * np.log10(np.maximum(spectrum, 1e-20))


def make_backend(state: RadioState, force_sim: bool = False) -> Backend:
    """Return the GNU Radio backend when it can be imported, else the simulator."""
    reason = "--sim requested"
    if not force_sim:
        try:
            from .flowgraph import GnuRadioBackend  # noqa: WPS433 (import inside function on purpose)
            backend = GnuRadioBackend(state)
            log.info("Using GNU Radio backend")
            return backend
        except Exception as exc:  # ImportError or device open failure
            log.exception("GNU Radio backend unavailable; using simulator")
            reason = f"{type(exc).__name__}: {exc}"
    from .backend_sim import SimBackend
    return SimBackend(state, reason=reason)
