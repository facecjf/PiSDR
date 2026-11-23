"""Backend interface shared by the GNU Radio flowgraph and the simulator.

A backend owns the hardware centre frequency. The UI only ever talks in terms of the VFO
frequency; the backend decides when to retune the dongle so the VFO stays inside the
visible span and away from the DC spike. :attr:`center_changed` announces the *displayed*
window, which is the full span at zoom x1 or a slice around the VFO when zoomed in.

It also owns the choice of dongle. With two attached (see :mod:`pisdr.radio.tuners`) the
receiver moves to whichever covers the VFO frequency, and the ADS-B decoder runs on the
second one while the receiver carries on with the first.
"""
from __future__ import annotations

import logging
import wave

import numpy as np

from .. import config
from ..qt import QObject, QTimer, Signal
from ..state import RadioState
from .decoders import Multimon
from .tuners import Lineup, Source

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

    def __init__(self, state: RadioState, parent: QObject | None = None,
                 lineup: Lineup | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.span_hz = float(config.SAMPLE_RATE)
        self.center_hz: float = 0.0
        self.view_center_hz: float = 0.0
        self.view_span_hz: float = self.span_hz
        self.running = False
        self.radio_down = False   # set when the dongle could not be reopened
        self._spectrum_on = True
        self.lineup = lineup or Lineup()
        self._aux_busy = False    # the ADS-B decoder has the second dongle
        self._adsb_lent = False   # ... or it has the receiver's, and the receiver is paused
        self._deaf = False        # the VFO is somewhere no attached dongle tunes
        self.source: Source = self._route()
        self._publish_source()
        state.adsb_own_dongle = self.lineup.aux is not None
        self.multimon = Multimon(self)
        self.multimon.line.connect(state.append_decoder_line)
        self._adsb_timer = QTimer(self)
        self._adsb_timer.setInterval(config.ADSB_POLL_MS)
        self._adsb_timer.timeout.connect(self.poll_adsb)
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
        self._adsb_timer.stop()
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
        if not self.running:
            return
        if self.radio_down:
            # A dongle would not open. Tuning to where the other one is used is the way out.
            if field in ("vfo_hz", "tuner_pref") and self._route() != self.source:
                self._switch_source(self._route())
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
        elif field == "tuner_pref":
            self._retune_for_vfo()
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

    # ------------------------------------------------------------------ which dongle
    def _route(self) -> Source:
        """The dongle (and the way of opening it) that should receive the VFO frequency."""
        return self.lineup.route(self.state.vfo_hz, self.state.tuner_pref, aux_free=not self._aux_busy)

    def _publish_source(self) -> None:
        self.state.set_tuner(self.source.label, self.source.dongle.spec.gain_max_db)

    def _switch_source(self, source: Source) -> None:
        """Move the receiver to another dongle, or reopen the same one the other way."""
        self.source = source
        self._publish_source()
        self.status.emit(f"Tuner: {source.label}")
        error = self._hw_select_source(source)
        if error:
            self.status.emit(error)
            return
        self._apply_all()

    def _note_coverage(self) -> None:
        """Say so, once, when the VFO goes where the dongle in use cannot follow."""
        deaf = not self.source.covers(self.state.vfo_hz)
        if deaf and not self._deaf:
            busy = " (the other tuner is on ADS-B)" if self._aux_busy else ""
            self.status.emit(f"{self.state.vfo_hz / 1e6:.3f} MHz is outside the {self.source.label} tuner's range{busy}")
        self._deaf = deaf

    def _retune_for_vfo(self, force: bool = False) -> None:
        source = self._route()
        if source != self.source:
            self._switch_source(source)       # ends in _apply_all, which comes back here
            return
        self._note_coverage()
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
        if self.state.dongle_lent:
            return "Leave network mode / the ADS-B map first"
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

    # ------------------------------------------------------------------ lending the dongle out
    def _pause_receiver(self) -> None:
        """Stop everything that uses the receiver before the dongle goes to another program."""
        self.stop_playback()
        if self.state.recording:
            self.state.set_recording(False)
        self.multimon.stop()
        self.running = False
        self.state.set_receiver_paused(True)

    def _resume_receiver(self) -> None:
        self.running = True
        self.state.set_receiver_paused(False)
        self._apply_all()

    def _took_back(self, error: str | None) -> str | None:
        self._resume_receiver()
        if self.radio_down:
            return (error or "Radio did not reopen") + " - restart PiSDR"
        return error

    # ------------------------------------------------------------------ network mode
    def start_network_mode(self) -> str | None:
        """Release the receiver's dongle to rtl_tcp. Returns an error text or None."""
        if self.state.network_mode:
            return None
        if self.state.adsb_mode:
            return "Leave the ADS-B map first"
        self._pause_receiver()
        error = self._hw_network(True)
        if error:
            self._resume_receiver()
            return error
        self.state.set_network_mode(True)
        return None

    def stop_network_mode(self) -> str | None:
        if not self.state.network_mode:
            return None
        error = self._hw_network(False)
        self.state.set_network_mode(False)
        return self._took_back(error)

    # ------------------------------------------------------------------ ADS-B mode
    def start_adsb_mode(self) -> str | None:
        """Start the ADS-B decoder on its dongle. Returns an error text or None.

        With two dongles the receiver keeps playing on the main one. It is only paused when
        the decoder's dongle is the one it needs: there is just one, or the VFO is where
        only the second dongle tunes (above 1766 MHz).
        """
        if self.state.adsb_mode:
            return None
        if self.state.network_mode:
            return "Leave network mode first"
        problem = self._hw_adsb_problem()
        if problem:
            return problem                         # nothing to run: leave the radio alone
        self._aux_busy = self.lineup.aux is not None
        meanwhile = self._route()                  # where the receiver can be while the map is open
        lent = self.source.dongle == self.lineup.adsb and (
            meanwhile.dongle == self.lineup.adsb or not meanwhile.covers(self.state.vfo_hz))
        if lent:
            self._pause_receiver()
            self._hw_release()
        elif meanwhile != self.source:
            self._switch_source(meanwhile)         # off the decoder's dongle, onto the main tuner
        error = self._hw_adsb(True)
        if error:
            self._aux_busy = False
            if lent:
                reopen = self._hw_reopen()
                self._resume_receiver()
                return f"{error}; {reopen}" if reopen else error
            self._resettle()
            return error
        self._adsb_lent = lent
        self.state.set_aircraft([])
        self.state.set_adsb_mode(True)
        self._adsb_timer.start()
        return None

    def stop_adsb_mode(self) -> str | None:
        if not self.state.adsb_mode:
            return None
        self._adsb_timer.stop()
        self._hw_adsb(False)
        self._aux_busy = False
        self.state.set_adsb_mode(False)
        self.state.set_aircraft([])
        if self._adsb_lent:
            self._adsb_lent = False
            return self._took_back(self._hw_reopen())
        self._resettle()
        return None

    def _resettle(self) -> None:
        """The second dongle is free again: move the receiver back to it if it was moved off."""
        if self.running and self._route() != self.source:
            self._switch_source(self._route())

    def waits_for_adsb(self, hz: float) -> bool:
        """True if *hz* cannot be received while the ADS-B map is open but can once it is
        closed: only the dongle the decoder is using tunes it. (The record timer asks.)"""
        if not self._aux_busy:
            return False
        prefer = self.state.tuner_pref
        return not self.lineup.route(hz, prefer, aux_free=False).covers(hz) and self.lineup.route(hz, prefer).covers(hz)

    def poll_adsb(self) -> None:
        """Once a second in ADS-B mode: copy the decoder's aircraft list into the state."""
        if self.state.adsb_mode:
            self.state.set_aircraft(*self._hw_adsb_poll())

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

    def _hw_select_source(self, source: Source) -> str | None:
        """Close the dongle in use and open *source*. Returns an error text or None."""

    def _hw_release(self) -> None:
        """Close the receiver's dongle so that another program can open it."""

    def _hw_reopen(self) -> str | None:
        """Open the receiver's dongle again after :meth:`_hw_release`. Error text or None."""

    def _hw_adsb_problem(self) -> str | None:
        """Why the ADS-B decoder cannot be started at all (not installed ...), or None."""

    def _hw_adsb(self, on: bool) -> str | None:
        """Start (error text or None) or stop the ADS-B decoder on ``self.lineup.adsb``."""

    def _hw_adsb_poll(self) -> tuple[list, int, str]:
        """(aircraft, messages decoded so far, error text if the decoder has stopped)."""
        return [], 0, ""

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
            from . import tuners
            from .flowgraph import GnuRadioBackend  # noqa: WPS433 (import inside function on purpose)
            backend = GnuRadioBackend(state, lineup=tuners.detect())
            log.info("Using GNU Radio backend")
            return backend
        except Exception as exc:  # ImportError or device open failure
            log.exception("GNU Radio backend unavailable; using simulator")
            reason = f"{type(exc).__name__}: {exc}"
    from .backend_sim import SimBackend
    return SimBackend(state, reason=reason)
