"""Runtime radio state shared by the UI, the physical controls and the backend.

:class:`RadioState` is the single source of truth. Anything that wants to change the radio
calls a method here; anything that wants to react connects to :attr:`RadioState.changed`,
which carries the name of the field that changed. Persistence to ``settings.json`` is
debounced so turning the knob does not hammer the SD card.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import logging
import time

from . import config
from .config import Band, KNOB_FUNCTIONS, MODES, STEPS_HZ
from .memories import Memory, default_name, load_memories, save_memories
from .qt import QObject, QTimer, Signal

log = logging.getLogger(__name__)


def _cycle(options: tuple, current, delta: int):
    """Next/previous entry of *options* after *current* (first entry if not present)."""
    i = options.index(current) if current in options else 0
    return options[(i + delta) % len(options)]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _num(value, default: float, lo: float, hi: float, cast=int):
    """A settings.json number clamped to [lo, hi]; *default* if it isn't a number."""
    try:
        return cast(_clamp(float(value), lo, hi))
    except (TypeError, ValueError):
        return cast(default)


class RadioState(QObject):
    """Observable radio state. Emit ``changed(field)`` on every modification."""

    changed = Signal(str)
    message = Signal(str)   # one-line status text for the UI

    PERSISTED = ("vfo_hz", "mode", "step_index", "volume", "squelch_db", "gain_db",
                 "agc", "bias_tee", "band_index", "fft_fps", "waterfall_speed",
                 "dsp_hpf", "dsp_hpf_hz", "dsp_lpf", "dsp_lpf_hz",
                 "dsp_notch", "dsp_notch_hz", "dsp_nr_level", "dsp_nb_level", "theme",
                 "hf_panel", "wx_lat", "wx_lon", "wx_units",
                 "filter_index", "ppm", "smeter_cal_db", "cw_pitch_hz",
                 "scan_dwell_ms", "scan_resume_ms", "scan_floor_db", "lockouts",
                 "memory_group", "memory_scan_on_now", "priority", "priority_interval_s",
                 "clock_utc", "brightness", "dim_after_s", "knob_accel",
                 "spectrum_zoom", "spectrum_avg", "peak_hold",
                 "spectrum_style", "wf_palette", "spectrum_labels", "snap_mode",
                 "timer_armed", "timer_start_min", "timer_length_min", "timer_daily", "timer_target",
                 "decoder", "rds_enabled")

    def __init__(self, bands: list[Band] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bands: list[Band] = bands or config.load_bands()
        self.band_index: int = 0
        first = self.bands[0]
        self.vfo_hz: int = first.default_hz
        self.mode: str = first.mode
        self.step_index: int = config.step_index_for(first.step_hz)
        self.volume: int = 60              # 0..100
        self.squelch_db: float = -100.0    # open
        self.gain_db: float = config.DEFAULT_GAIN_DB
        self.agc: bool = False
        self.bias_tee: bool = False
        self.recording: bool = False
        self.knob_function: str = "TUNE"
        self.fft_fps: int = config.FFT_FPS
        self.waterfall_speed: int = 1      # rows per frame
        self.signal_db: float = -120.0
        self.dsp_hpf: bool = True
        self.dsp_hpf_hz: int = config.DSP_HPF_HZ_DEFAULT
        self.dsp_lpf: bool = False
        self.dsp_lpf_hz: int = config.DSP_LPF_HZ_DEFAULT
        self.dsp_notch: bool = False
        self.dsp_notch_hz: int = config.DSP_NOTCH_HZ_DEFAULT
        self.dsp_nr_level: int = 0         # 0 = off, 1..10
        self.dsp_nb_level: int = 0         # 0 = off, 1..10
        self.memories: list[Memory] = load_memories()
        self.memory_index: int = 0
        self.scanning: bool = False
        self.scan_held: bool = False
        self.scan_source: str = "band"     # "band" or "memory"
        self.cw_text: str = ""             # decoded Morse, newest at the end
        self.cw_wpm: float = 0.0
        self.theme: str = config.DEFAULT_THEME
        self.hf_panel: str = "AUTO"        # OFF / ON / AUTO (shown while tuned below 30 MHz)
        self.wx_lat: float | None = None   # weather location; None = IP geolocation
        self.wx_lon: float | None = None
        self.wx_units: str = "imperial"    # or "metric"
        self.space_wx = None               # spacewx.SpaceWx, set by the fetcher

        # Receiver settings
        self.filter_index: dict[str, int] = dict(config.DEFAULT_FILTER_INDEX)
        self.ppm: int = config.PPM_CORRECTION
        self.smeter_cal_db: float = config.SMETER_CAL_DB
        self.cw_pitch_hz: int = int(config.CW_PITCH_HZ)

        # Scanning
        self.scan_dwell_ms: int = config.SCAN_DWELL_MS
        self.scan_resume_ms: int = config.SCAN_RESUME_MS
        self.scan_floor_db: float = config.SCAN_FLOOR_DB
        self.lockouts: list[int] = []
        self.memory_group: str = config.MEMORY_GROUP_ALL
        self.memory_scan_on_now: bool = False
        self.priority: dict | None = None  # {"name", "vfo_hz", "mode", "step_hz"}
        self.priority_interval_s: int = 3
        self.scan_log: list[dict] = _load_scan_log()

        # Display / controls
        self.clock_utc: bool = False
        self.brightness: int = 100         # percent
        self.dim_after_s: int = 0          # 0 = never
        self.knob_accel: bool = True
        self.spectrum_zoom: int = 1
        self.spectrum_avg: int = config.DEFAULT_SPECTRUM_AVG
        self.peak_hold: bool = False
        self.spectrum_style: str = config.SPECTRUM_STYLES[0]
        self.wf_palette: str = config.WATERFALL_PALETTES[0]
        self.spectrum_labels: str = config.SPECTRUM_LABELS[0]
        self.snap_mode: str = config.SNAP_MODES[0]

        # Record timer (times are UTC minutes after midnight)
        self.timer_armed: bool = False
        self.timer_start_min: int = 0
        self.timer_length_min: int = 30
        self.timer_daily: bool = False
        self.timer_target: dict | None = None   # what to tune when the timer fires
        self.timer_running: bool = False

        # Decoders / RDS / playback / network (not persisted unless listed above)
        self.decoder: str = "OFF"
        self.decoder_lines: list[str] = []
        self.rds_enabled: bool = True
        self.rds_ps: str = ""
        self.rds_rt: str = ""
        self.rds_pty: str = ""
        self.playing: str = ""             # path of the recording being played back
        self.network_mode: bool = False

        self._func_last_used = time.monotonic()
        self._knob_hist: collections.deque = collections.deque()
        self._knob_dir = 0

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2000)
        self._save_timer.timeout.connect(self.save)

        self._func_timer = QTimer(self)
        self._func_timer.setInterval(500)
        self._func_timer.timeout.connect(self._check_func_return)
        self._func_timer.start()

    # ------------------------------------------------------------------ helpers
    @property
    def step_hz(self) -> int:
        return STEPS_HZ[self.step_index]

    @property
    def band(self) -> Band:
        return self.bands[self.band_index]

    @property
    def filter_hz(self) -> int:
        widths = config.FILTER_WIDTHS[self.mode]
        index = self.filter_index.get(self.mode, config.DEFAULT_FILTER_INDEX[self.mode])
        return widths[int(_clamp(index, 0, len(widths) - 1))]

    @property
    def mode_spec(self) -> config.ModeSpec:
        """MODE_SPECS for the current mode with the selected filter width applied."""
        base = config.MODE_SPECS[self.mode]
        bw = float(self.filter_hz)
        offset = base.offset_hz
        if self.mode in ("USB", "LSB"):
            offset = (config.SSB_LOW_CUT_HZ + bw / 2) * (1 if self.mode == "USB" else -1)
        return dataclasses.replace(base, bandwidth_hz=bw, offset_hz=offset)

    def _set(self, field: str, value, persist: bool = True) -> None:
        if getattr(self, field) == value:
            return
        setattr(self, field, value)
        self.changed.emit(field)
        if persist and field in self.PERSISTED:
            self._save_timer.start()

    # ------------------------------------------------------------------ tuning
    def set_vfo(self, hz: float) -> None:
        hz = int(round(max(config.FREQ_MIN, min(config.FREQ_MAX, hz))))
        self._set("vfo_hz", hz)
        # Keep band_index in sync when tuning wanders into another band.
        for i, band in enumerate(self.bands):
            if band.contains(hz):
                if i != self.band_index:
                    self._set("band_index", i)
                break

    def tune_steps(self, n: int) -> None:
        """Move the VFO by *n* steps, snapping to the step grid like a real radio."""
        step = self.step_hz
        base = round(self.vfo_hz / step) * step
        self.set_vfo(base + n * step)

    def set_step_index(self, index: int) -> None:
        self._set("step_index", index % len(STEPS_HZ))

    def cycle_step(self) -> None:
        self.set_step_index(self.step_index + 1)
        self.message.emit(f"Step {format_step(self.step_hz)}")

    def set_step_hz(self, hz: int) -> None:
        self.set_step_index(config.step_index_for(hz))

    def tune_to(self, vfo_hz: int, mode: str, step_hz: int) -> None:
        """Mode, step and frequency in the order that avoids a needless retune."""
        self.set_mode(mode)
        self.set_step_hz(step_hz)
        self.set_vfo(vfo_hz)

    # ------------------------------------------------------------------ mode / band
    def set_mode(self, mode: str) -> None:
        if mode in MODES:
            self._set("mode", mode)

    def cycle_mode(self) -> None:
        self.set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)])
        self.message.emit(f"Mode {self.mode}")

    def select_band(self, index: int) -> None:
        index %= len(self.bands)
        band = self.bands[index]
        self._set("band_index", index)
        self.set_mode(band.mode)
        self.set_step_hz(band.step_hz)
        self.set_vfo(band.default_hz)
        self.message.emit(f"Band {band.name}")

    def cycle_band(self) -> None:
        self.select_band(self.band_index + 1)

    # ------------------------------------------------------------------ receiver settings
    def step_filter(self, delta: int) -> None:
        widths = config.FILTER_WIDTHS[self.mode]
        current = self.filter_index.get(self.mode, config.DEFAULT_FILTER_INDEX[self.mode])
        self._set_filter_index((current + (delta or 1)) % len(widths))

    def fit_filter(self, width_hz: float) -> None:
        """Narrowest filter for the current mode that still passes *width_hz*."""
        widths = config.FILTER_WIDTHS[self.mode]
        fits = [i for i, w in enumerate(widths) if w >= width_hz]
        self._set_filter_index(fits[0] if fits else len(widths) - 1)

    def _set_filter_index(self, index: int) -> None:
        if index != self.filter_index.get(self.mode):
            self.filter_index = {**self.filter_index, self.mode: index}
            self.changed.emit("filter_index")
            self._save_timer.start()

    def set_ppm(self, ppm: float) -> None:
        self._set("ppm", int(_clamp(round(ppm), config.PPM_MIN, config.PPM_MAX)))

    def set_smeter_cal(self, db: float) -> None:
        self._set("smeter_cal_db", float(_clamp(db, config.SMETER_CAL_MIN_DB, config.SMETER_CAL_MAX_DB)))

    def set_cw_pitch(self, hz: float) -> None:
        self._set("cw_pitch_hz", int(_clamp(hz, config.CW_PITCH_MIN_HZ, config.CW_PITCH_MAX_HZ)))

    # ------------------------------------------------------------------ knob function
    def set_knob_function(self, name: str) -> None:
        if name in KNOB_FUNCTIONS:
            self._func_last_used = time.monotonic()
            self._set("knob_function", name, persist=False)

    def cycle_knob_function(self) -> None:
        i = KNOB_FUNCTIONS.index(self.knob_function)
        self.set_knob_function(KNOB_FUNCTIONS[(i + 1) % len(KNOB_FUNCTIONS)])

    def knob_turned(self, detents: int) -> None:
        """Apply *detents* (+/-) of encoder movement to whatever the knob controls."""
        self._func_last_used = time.monotonic()
        fn = self.knob_function
        if fn == "TUNE":
            self.tune_steps(detents * self._accel_factor(detents))
        elif fn == "VOLUME":
            self.set_volume(self.volume + detents * config.VOLUME_STEP)
        elif fn == "SQUELCH":
            self.set_squelch(self.squelch_db + detents * config.SQUELCH_STEP_DB)
        elif fn == "GAIN":
            self.set_gain(self.gain_db + detents * config.GAIN_STEP_DB)

    def _accel_factor(self, detents: int) -> int:
        """Step multiplier from how fast the knob is spinning (1 when acceleration is off)."""
        if not self.knob_accel or detents == 0:
            return 1
        now = time.monotonic()
        direction = 1 if detents > 0 else -1
        if direction != self._knob_dir:
            self._knob_hist.clear()
            self._knob_dir = direction
        self._knob_hist.append((now, abs(detents)))
        cutoff = now - config.KNOB_ACCEL_WINDOW_S
        while self._knob_hist and self._knob_hist[0][0] < cutoff:
            self._knob_hist.popleft()
        rate = sum(n for _, n in self._knob_hist) / config.KNOB_ACCEL_WINDOW_S
        for threshold, factor in config.KNOB_ACCEL_STAGES:
            if rate >= threshold:
                return factor
        return 1

    def _check_func_return(self) -> None:
        if self.knob_function != "TUNE" and time.monotonic() - self._func_last_used > config.FUNC_RETURN_S:
            self._set("knob_function", "TUNE", persist=False)

    # ------------------------------------------------------------------ audio / RF
    def set_volume(self, pct: float) -> None:
        self._set("volume", int(max(0, min(100, pct))))

    def set_squelch(self, db: float) -> None:
        self._set("squelch_db", float(max(config.SQUELCH_MIN_DB, min(config.SQUELCH_MAX_DB, db))))

    def set_gain(self, db: float) -> None:
        self._set("gain_db", float(max(config.GAIN_MIN_DB, min(config.GAIN_MAX_DB, db))))
        if self.agc:
            self._set("agc", False)

    def set_agc(self, on: bool) -> None:
        self._set("agc", bool(on))

    def set_bias_tee(self, on: bool) -> None:
        self._set("bias_tee", bool(on))
        self.message.emit("Bias tee ON (4.5 V on antenna port)" if on else "Bias tee off")

    def set_recording(self, on: bool) -> None:
        if on and (self.playing or self.network_mode):
            self.message.emit("Stop playback / network mode first")
            return
        self._set("recording", bool(on), persist=False)

    def toggle_recording(self) -> None:
        self.set_recording(not self.recording)

    def set_signal_db(self, db: float) -> None:
        self.signal_db = db
        self.changed.emit("signal_db")

    def set_fft_fps(self, fps: int) -> None:
        self._set("fft_fps", int(max(5, min(40, fps))))

    def set_waterfall_speed(self, rows: int) -> None:
        self._set("waterfall_speed", int(max(1, min(4, rows))))

    # ------------------------------------------------------------------ display
    def set_clock_utc(self, on: bool) -> None:
        self._set("clock_utc", bool(on))

    def set_brightness(self, pct: float) -> None:
        self._set("brightness", int(_clamp(round(pct / 10) * 10, config.BRIGHTNESS_MIN_PCT, 100)))

    def cycle_dim_after(self, delta: int = 1) -> None:
        self._set("dim_after_s", _cycle(config.DIM_AFTER_CHOICES_S, self.dim_after_s, delta))

    def set_knob_accel(self, on: bool) -> None:
        self._set("knob_accel", bool(on))

    def cycle_spectrum_zoom(self, delta: int = 1) -> None:
        self._set("spectrum_zoom", _cycle(config.SPECTRUM_ZOOMS, self.spectrum_zoom, delta))

    def step_spectrum_zoom(self, delta: int) -> None:
        """Zoom in (+1) or out (-1), stopping at the ends instead of wrapping."""
        zooms = config.SPECTRUM_ZOOMS
        i = zooms.index(self.spectrum_zoom) if self.spectrum_zoom in zooms else 0
        self._set("spectrum_zoom", zooms[max(0, min(len(zooms) - 1, i + delta))])

    def cycle_spectrum_style(self, delta: int = 1) -> None:
        self._set("spectrum_style", _cycle(config.SPECTRUM_STYLES, self.spectrum_style, delta))

    def cycle_wf_palette(self, delta: int = 1) -> None:
        self._set("wf_palette", _cycle(config.WATERFALL_PALETTES, self.wf_palette, delta))

    def cycle_spectrum_labels(self, delta: int = 1) -> None:
        self._set("spectrum_labels", _cycle(config.SPECTRUM_LABELS, self.spectrum_labels, delta))

    def cycle_snap_mode(self, delta: int = 1) -> None:
        self._set("snap_mode", _cycle(config.SNAP_MODES, self.snap_mode, delta))

    def cycle_spectrum_avg(self, delta: int = 1) -> None:
        self._set("spectrum_avg", (self.spectrum_avg + delta) % len(config.SPECTRUM_AVG_NAMES))

    def set_peak_hold(self, on: bool) -> None:
        self._set("peak_hold", bool(on))

    # ------------------------------------------------------------------ audio DSP
    def set_dsp_hpf(self, on: bool) -> None:
        self._set("dsp_hpf", bool(on))

    def set_dsp_hpf_hz(self, hz: float) -> None:
        self._set("dsp_hpf_hz", int(max(config.DSP_HPF_HZ_MIN, min(config.DSP_HPF_HZ_MAX, hz))))

    def set_dsp_lpf(self, on: bool) -> None:
        self._set("dsp_lpf", bool(on))

    def set_dsp_lpf_hz(self, hz: float) -> None:
        self._set("dsp_lpf_hz", int(max(config.DSP_LPF_HZ_MIN, min(config.DSP_LPF_HZ_MAX, hz))))

    def set_dsp_notch(self, on: bool) -> None:
        self._set("dsp_notch", bool(on))

    def set_dsp_notch_hz(self, hz: float) -> None:
        self._set("dsp_notch_hz", int(max(config.DSP_NOTCH_HZ_MIN, min(config.DSP_NOTCH_HZ_MAX, hz))))

    def set_dsp_nr_level(self, level: float) -> None:
        self._set("dsp_nr_level", int(max(0, min(config.DSP_LEVEL_MAX, level))))

    def set_dsp_nb_level(self, level: float) -> None:
        self._set("dsp_nb_level", int(max(0, min(config.DSP_LEVEL_MAX, level))))

    def toggle_dsp_hpf(self) -> None:
        self.set_dsp_hpf(not self.dsp_hpf)

    def toggle_dsp_lpf(self) -> None:
        self.set_dsp_lpf(not self.dsp_lpf)

    def toggle_dsp_notch(self) -> None:
        self.set_dsp_notch(not self.dsp_notch)

    # ------------------------------------------------------------------ theme
    def set_theme(self, name: str) -> None:
        if name in config.THEME_NAMES:
            self._set("theme", name)

    def cycle_theme(self, delta: int = 1) -> None:
        names = config.THEME_NAMES
        i = names.index(self.theme) if self.theme in names else 0
        self.set_theme(names[(i + delta) % len(names)])

    # ------------------------------------------------------------------ HF conditions panel
    @property
    def hf_panel_visible(self) -> bool:
        if self.hf_panel == "ON":
            return True
        if self.hf_panel == "AUTO":
            return self.vfo_hz <= config.HF_AUTO_MAX_HZ
        return False

    def set_hf_panel(self, mode: str) -> None:
        if mode in config.HF_PANEL_MODES:
            self._set("hf_panel", mode)

    def cycle_hf_panel(self, delta: int = 1) -> None:
        modes = config.HF_PANEL_MODES
        i = modes.index(self.hf_panel) if self.hf_panel in modes else 0
        self.set_hf_panel(modes[(i + delta) % len(modes)])

    def toggle_hf_panel(self) -> None:
        """Top-bar tap: hide the panel if it is showing, otherwise pin it on."""
        self.set_hf_panel("OFF" if self.hf_panel_visible else "ON")
        self.message.emit("HF conditions " + ("hidden" if self.hf_panel == "OFF" else "shown"))

    def set_space_wx(self, data) -> None:
        self.space_wx = data
        self.changed.emit("space_wx")

    # ------------------------------------------------------------------ CW decoder
    def append_cw_text(self, text: str) -> None:
        if not text:
            return
        if text == " " and (not self.cw_text or self.cw_text.endswith(" ")):
            return
        self.cw_text = (self.cw_text + text)[-config.CW_TEXT_MAX:]
        self.changed.emit("cw_text")

    def clear_cw_text(self) -> None:
        if self.cw_text:
            self.cw_text = ""
            self.changed.emit("cw_text")

    def set_cw_wpm(self, wpm: float) -> None:
        wpm = round(float(wpm), 1)
        if wpm != self.cw_wpm:
            self.cw_wpm = wpm
            self.changed.emit("cw_wpm")

    # ------------------------------------------------------------------ data decoders / RDS
    def set_decoder(self, name: str) -> None:
        if name in config.DECODERS:
            self._set("decoder", name)

    def cycle_decoder(self, delta: int = 1) -> None:
        self.set_decoder(_cycle(tuple(config.DECODERS), self.decoder, delta))

    def append_decoder_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.decoder_lines = (self.decoder_lines + [line])[-config.DECODER_TEXT_MAX:]
        self.changed.emit("decoder_lines")

    def clear_decoder_lines(self) -> None:
        if self.decoder_lines:
            self.decoder_lines = []
            self.changed.emit("decoder_lines")

    def set_rds_enabled(self, on: bool) -> None:
        self._set("rds_enabled", bool(on))
        if not on:
            self.clear_rds()

    def set_rds(self, field: str, text: str) -> None:
        """field is "ps" (station name), "rt" (radio text) or "pty" (programme type)."""
        if self.mode != "WFM" or not self.rds_enabled:
            return   # late message from the station we just left
        attr = f"rds_{field}"
        if hasattr(self, attr) and getattr(self, attr) != text:
            setattr(self, attr, text)
            self.changed.emit("rds")

    def clear_rds(self) -> None:
        if self.rds_ps or self.rds_rt or self.rds_pty:
            self.rds_ps = self.rds_rt = self.rds_pty = ""
            self.changed.emit("rds")

    # ------------------------------------------------------------------ playback / network
    def set_playing(self, path: str) -> None:
        self._set("playing", str(path or ""), persist=False)

    def set_network_mode(self, on: bool) -> None:
        self._set("network_mode", bool(on), persist=False)

    # ------------------------------------------------------------------ memories
    @property
    def memory_groups(self) -> list[str]:
        groups = sorted({m.group for m in self.memories if m.group})
        return [config.MEMORY_GROUP_ALL, *groups]

    def set_memory_group(self, group: str) -> None:
        if group not in self.memory_groups:
            group = config.MEMORY_GROUP_ALL
        self._set("memory_group", group)

    def cycle_memory_group(self, delta: int = 1) -> None:
        self.set_memory_group(_cycle(tuple(self.memory_groups), self.memory_group, delta))

    def memory_indices(self, on_now_only: bool = False) -> list[int]:
        """Indices into :attr:`memories` for the selected group (optionally only "on now")."""
        group = self.memory_group
        now = time.time()
        return [i for i, m in enumerate(self.memories)
                if (group == config.MEMORY_GROUP_ALL or m.group == group)
                and (not on_now_only or m.on_now(now))]

    def set_memory_scan_on_now(self, on: bool) -> None:
        self._set("memory_scan_on_now", bool(on))

    def save_current_memory(self, name: str | None = None) -> Memory | None:
        if len(self.memories) >= config.MAX_MEMORIES:
            self.message.emit(f"Memory full ({config.MAX_MEMORIES})")
            return None
        group = "" if self.memory_group == config.MEMORY_GROUP_ALL else self.memory_group
        mem = Memory(
            name=name or default_name(self.vfo_hz, self.mode),
            vfo_hz=self.vfo_hz,
            mode=self.mode,
            step_hz=self.step_hz,
            group=group,
        )
        self.memories.append(mem)
        self.memory_index = len(self.memories) - 1
        save_memories(self.memories)
        self.changed.emit("memories")
        self.message.emit(f"Saved {mem.name}")
        return mem

    def import_preset_memories(self, path=None) -> int:
        """Merge the bundled preset list into memories. Existing entries with the same
        frequency and mode get the preset's group and schedule if they have none."""
        presets = load_memories(path or config.PRESET_MEMORIES_FILE)
        if not presets:
            self.message.emit("No presets found")
            return 0
        have = {(m.vfo_hz, m.mode): m for m in self.memories}
        added = updated = 0
        for preset in presets:
            existing = have.get((preset.vfo_hz, preset.mode))
            if existing is not None:
                if not existing.group and (preset.group or preset.utc):
                    existing.group, existing.utc, existing.days = preset.group, preset.utc, preset.days
                    updated += 1
                continue
            if len(self.memories) >= config.MAX_MEMORIES:
                continue
            self.memories.append(preset)
            have[(preset.vfo_hz, preset.mode)] = preset
            added += 1
        if added or updated:
            save_memories(self.memories)
            self.changed.emit("memories")
        if added or updated:
            self.message.emit(f"Presets: {added} added, {updated} grouped")
        else:
            self.message.emit("Presets already loaded")
        return added

    def delete_memory(self, index: int) -> None:
        if not self.memories:
            return
        index %= len(self.memories)
        removed = self.memories.pop(index)
        self.memory_index = min(self.memory_index, max(0, len(self.memories) - 1))
        save_memories(self.memories)
        if self.memory_group not in self.memory_groups:
            self.set_memory_group(config.MEMORY_GROUP_ALL)
        self.changed.emit("memories")
        self.message.emit(f"Deleted {removed.name}")

    def recall_memory(self, index: int, announce: bool = True) -> None:
        if not self.memories:
            self.message.emit("No memories")
            return
        index %= len(self.memories)
        mem = self.memories[index]
        self.memory_index = index
        self.tune_to(mem.vfo_hz, mem.mode, mem.step_hz)
        if announce:
            self.message.emit(f"Memory {mem.name}")

    def name_for(self, vfo_hz: int, mode: str) -> str:
        for mem in self.memories:
            if mem.vfo_hz == vfo_hz and mem.mode == mode:
                return mem.name
        return default_name(vfo_hz, mode)

    # ------------------------------------------------------------------ scanning
    def set_scan_flags(self, scanning: bool, held: bool = False) -> None:
        self.scanning = scanning
        self.scan_held = held if scanning else False
        self.changed.emit("scanning")

    def set_scan_source(self, source: str) -> None:
        if source in ("band", "memory"):
            self.scan_source = source
            self.changed.emit("scan_source")

    def cycle_scan_source(self) -> None:
        self.set_scan_source("memory" if self.scan_source == "band" else "band")

    def cycle_scan_dwell(self, delta: int = 1) -> None:
        self._set("scan_dwell_ms", _cycle(config.SCAN_DWELL_CHOICES_MS, self.scan_dwell_ms, delta))

    def cycle_scan_resume(self, delta: int = 1) -> None:
        self._set("scan_resume_ms", _cycle(config.SCAN_RESUME_CHOICES_MS, self.scan_resume_ms, delta))

    def set_scan_floor(self, db: float) -> None:
        self._set("scan_floor_db", float(_clamp(db, config.SCAN_FLOOR_MIN_DB, config.SCAN_FLOOR_MAX_DB)))

    def is_locked_out(self, vfo_hz: int) -> bool:
        tol = max(1.0, self.mode_spec.bandwidth_hz / 2)
        return any(abs(vfo_hz - hz) < tol for hz in self.lockouts)

    def lock_out(self, vfo_hz: int) -> None:
        if self.is_locked_out(vfo_hz):
            return
        self.lockouts = (self.lockouts + [int(vfo_hz)])[-config.MAX_LOCKOUTS:]
        self.changed.emit("lockouts")
        self._save_timer.start()

    def toggle_lockout(self, vfo_hz: int) -> bool:
        """Lock out *vfo_hz*, or remove an existing lockout. Returns True if now locked out."""
        if not self.is_locked_out(vfo_hz):
            self.lock_out(vfo_hz)
            return True
        tol = max(1.0, self.mode_spec.bandwidth_hz / 2)
        self.lockouts = [hz for hz in self.lockouts if abs(vfo_hz - hz) >= tol]
        self.changed.emit("lockouts")
        self._save_timer.start()
        return False

    def clear_lockouts(self) -> None:
        if self.lockouts:
            n = len(self.lockouts)
            self.lockouts = []
            self.changed.emit("lockouts")
            self._save_timer.start()
            self.message.emit(f"Cleared {n} lockouts")

    def set_priority(self, target: dict | None) -> None:
        self._set("priority", dict(target) if target else None)

    def priority_from_current(self) -> dict:
        return {"name": self.name_for(self.vfo_hz, self.mode), "vfo_hz": self.vfo_hz,
                "mode": self.mode, "step_hz": self.step_hz}

    def cycle_priority(self, delta: int = 1) -> None:
        """OFF -> memory 1 -> memory 2 ... -> OFF."""
        options: list[dict | None] = [None] + [
            {"name": m.name, "vfo_hz": m.vfo_hz, "mode": m.mode, "step_hz": m.step_hz}
            for m in self.memories]
        current = 0
        if self.priority:
            for i, opt in enumerate(options[1:], start=1):
                if opt["vfo_hz"] == self.priority.get("vfo_hz") and opt["mode"] == self.priority.get("mode"):
                    current = i
                    break
        self.set_priority(options[(current + delta) % len(options)])

    def cycle_priority_interval(self, delta: int = 1) -> None:
        self._set("priority_interval_s",
                  _cycle(config.PRIORITY_INTERVAL_CHOICES_S, self.priority_interval_s, delta))

    def add_scan_hit(self, hit: dict) -> None:
        self.scan_log = ([hit] + self.scan_log)[: config.SCAN_LOG_MAX]
        _save_scan_log(self.scan_log)
        self.changed.emit("scan_log")

    def clear_scan_log(self) -> None:
        if self.scan_log:
            self.scan_log = []
            _save_scan_log(self.scan_log)
            self.changed.emit("scan_log")
            self.message.emit("Scan log cleared")

    # ------------------------------------------------------------------ record timer
    def adjust_timer_start(self, minutes: int) -> None:
        self._set("timer_start_min", (self.timer_start_min + minutes) % (24 * 60))

    def cycle_timer_length(self, delta: int = 1) -> None:
        self._set("timer_length_min",
                  _cycle(config.REC_TIMER_LENGTH_CHOICES_MIN, self.timer_length_min, delta))

    def set_timer_daily(self, on: bool) -> None:
        self._set("timer_daily", bool(on))

    def arm_timer(self, on: bool) -> None:
        """Arming captures the current frequency and mode as the recording target."""
        if on:
            self._set("timer_target", self.priority_from_current())
        self._set("timer_armed", bool(on))

    def set_timer_running(self, on: bool) -> None:
        self._set("timer_running", bool(on), persist=False)

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        try:
            config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {k: getattr(self, k) for k in self.PERSISTED}
            with open(config.SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def load(self) -> None:
        try:
            with open(config.SETTINGS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        defaults = {k: getattr(self, k) for k in self.PERSISTED}
        for key in self.PERSISTED:
            if key in data:
                setattr(self, key, data[key])
        try:
            self._validate()
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("settings.json has bad values (%s); using defaults for new settings", exc)
            for key, value in defaults.items():
                if key not in ("vfo_hz", "mode", "band_index", "step_index"):
                    setattr(self, key, value)
            self._validate()
        for key in self.PERSISTED:
            self.changed.emit(key)

    def _validate(self) -> None:
        self.band_index = _num(self.band_index, 0, 0, len(self.bands) - 1)
        self.step_index = _num(self.step_index, 0, 0, len(STEPS_HZ) - 1)
        if self.mode not in MODES:
            self.mode = MODES[0]
        self.vfo_hz = _num(self.vfo_hz, self.bands[self.band_index].default_hz,
                           config.FREQ_MIN, config.FREQ_MAX)
        self.dsp_hpf = bool(self.dsp_hpf)
        self.dsp_lpf = bool(self.dsp_lpf)
        self.dsp_notch = bool(self.dsp_notch)
        self.dsp_hpf_hz = _num(self.dsp_hpf_hz, config.DSP_HPF_HZ_DEFAULT,
                               config.DSP_HPF_HZ_MIN, config.DSP_HPF_HZ_MAX)
        self.dsp_lpf_hz = _num(self.dsp_lpf_hz, config.DSP_LPF_HZ_DEFAULT,
                               config.DSP_LPF_HZ_MIN, config.DSP_LPF_HZ_MAX)
        self.dsp_notch_hz = _num(self.dsp_notch_hz, config.DSP_NOTCH_HZ_DEFAULT,
                                 config.DSP_NOTCH_HZ_MIN, config.DSP_NOTCH_HZ_MAX)
        self.dsp_nr_level = _num(self.dsp_nr_level, 0, 0, config.DSP_LEVEL_MAX)
        self.dsp_nb_level = _num(self.dsp_nb_level, 0, 0, config.DSP_LEVEL_MAX)
        if self.theme not in config.THEME_NAMES:
            self.theme = config.DEFAULT_THEME
        if self.hf_panel not in config.HF_PANEL_MODES:
            self.hf_panel = "AUTO"
        if self.wx_units not in ("imperial", "metric"):
            self.wx_units = "imperial"
        for key in ("wx_lat", "wx_lon"):
            value = getattr(self, key)
            setattr(self, key, float(value) if isinstance(value, (int, float)) else None)

        filt = dict(config.DEFAULT_FILTER_INDEX)
        if isinstance(self.filter_index, dict):
            for mode, index in self.filter_index.items():
                if mode in filt and isinstance(index, int):
                    filt[mode] = int(_clamp(index, 0, len(config.FILTER_WIDTHS[mode]) - 1))
        self.filter_index = filt
        self.ppm = _num(self.ppm, config.PPM_CORRECTION, config.PPM_MIN, config.PPM_MAX)
        self.smeter_cal_db = _num(self.smeter_cal_db, config.SMETER_CAL_DB,
                                  config.SMETER_CAL_MIN_DB, config.SMETER_CAL_MAX_DB, float)
        self.cw_pitch_hz = _num(self.cw_pitch_hz, config.CW_PITCH_HZ,
                                config.CW_PITCH_MIN_HZ, config.CW_PITCH_MAX_HZ)
        if self.scan_dwell_ms not in config.SCAN_DWELL_CHOICES_MS:
            self.scan_dwell_ms = config.SCAN_DWELL_MS
        if self.scan_resume_ms not in config.SCAN_RESUME_CHOICES_MS:
            self.scan_resume_ms = config.SCAN_RESUME_MS
        self.scan_floor_db = _num(self.scan_floor_db, config.SCAN_FLOOR_DB,
                                  config.SCAN_FLOOR_MIN_DB, config.SCAN_FLOOR_MAX_DB, float)
        lockouts = self.lockouts if isinstance(self.lockouts, list) else []
        self.lockouts = [int(hz) for hz in lockouts
                         if isinstance(hz, (int, float))][-config.MAX_LOCKOUTS:]
        self.memory_group = str(self.memory_group)
        if self.memory_group not in self.memory_groups:
            self.memory_group = config.MEMORY_GROUP_ALL
        self.memory_scan_on_now = bool(self.memory_scan_on_now)
        self.priority = _valid_target(self.priority)
        if self.priority_interval_s not in config.PRIORITY_INTERVAL_CHOICES_S:
            self.priority_interval_s = 3
        self.clock_utc = bool(self.clock_utc)
        self.brightness = _num(self.brightness, 100, config.BRIGHTNESS_MIN_PCT, 100)
        if self.dim_after_s not in config.DIM_AFTER_CHOICES_S:
            self.dim_after_s = 0
        self.knob_accel = bool(self.knob_accel)
        if self.spectrum_zoom not in config.SPECTRUM_ZOOMS:
            self.spectrum_zoom = 1
        self.spectrum_avg = _num(self.spectrum_avg, config.DEFAULT_SPECTRUM_AVG,
                                 0, len(config.SPECTRUM_AVG_NAMES) - 1)
        self.peak_hold = bool(self.peak_hold)
        for key, choices in (("spectrum_style", config.SPECTRUM_STYLES),
                             ("wf_palette", config.WATERFALL_PALETTES),
                             ("spectrum_labels", config.SPECTRUM_LABELS),
                             ("snap_mode", config.SNAP_MODES)):
            if getattr(self, key) not in choices:
                setattr(self, key, choices[0])
        self.timer_start_min = _num(self.timer_start_min, 0, 0, 24 * 60 - 1)
        if self.timer_length_min not in config.REC_TIMER_LENGTH_CHOICES_MIN:
            self.timer_length_min = 30
        self.timer_daily = bool(self.timer_daily)
        self.timer_target = _valid_target(self.timer_target)
        self.timer_armed = bool(self.timer_armed) and self.timer_target is not None
        if self.decoder not in config.DECODERS:
            self.decoder = "OFF"
        self.rds_enabled = bool(self.rds_enabled)


def _valid_target(value) -> dict | None:
    """A {"name", "vfo_hz", "mode", "step_hz"} dict from settings, or None if malformed."""
    if not isinstance(value, dict):
        return None
    try:
        mode = value["mode"] if value.get("mode") in MODES else "AM"
        vfo = int(_clamp(int(value["vfo_hz"]), config.FREQ_MIN, config.FREQ_MAX))
        return {"name": str(value.get("name") or default_name(vfo, mode)), "vfo_hz": vfo,
                "mode": mode, "step_hz": int(value.get("step_hz", 1000))}
    except (KeyError, TypeError, ValueError):
        return None


def _load_scan_log() -> list[dict]:
    try:
        with open(config.SCAN_LOG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [h for h in data.get("hits", []) if isinstance(h, dict)][: config.SCAN_LOG_MAX]
    except (OSError, ValueError, AttributeError):
        return []


def _save_scan_log(hits: list[dict]) -> None:
    try:
        config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.SCAN_LOG_FILE, "w", encoding="utf-8") as fh:
            json.dump({"hits": hits}, fh, indent=1)
    except OSError as exc:
        log.warning("Could not save scan log: %s", exc)


def format_step(hz: int) -> str:
    if hz >= 1_000_000:
        return f"{hz / 1e6:g} MHz"
    if hz >= 1_000:
        return f"{hz / 1e3:g} kHz"
    return f"{hz} Hz"


def format_freq(hz: float) -> str:
    """'14.200.000' style grouping used on the big display."""
    s = f"{int(round(hz)):010d}"
    return f"{s[0:4].lstrip('0') or '0'}.{s[4:7]}.{s[7:10]}"
