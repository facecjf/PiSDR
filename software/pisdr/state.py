"""Runtime radio state shared by the UI, the physical controls and the backend.

:class:`RadioState` is the single source of truth. Anything that wants to change the radio
calls a method here; anything that wants to react connects to :attr:`RadioState.changed`,
which carries the name of the field that changed. Persistence to ``settings.json`` is
debounced so turning the knob does not hammer the SD card.
"""
from __future__ import annotations

import json
import logging
import time

from . import config
from .config import Band, KNOB_FUNCTIONS, MODES, STEPS_HZ
from .memories import Memory, default_name, load_memories, save_memories
from .qt import QObject, QTimer, Signal

log = logging.getLogger(__name__)


class RadioState(QObject):
    """Observable radio state. Emit ``changed(field)`` on every modification."""

    changed = Signal(str)
    message = Signal(str)   # one-line status text for the UI

    PERSISTED = ("vfo_hz", "mode", "step_index", "volume", "squelch_db", "gain_db",
                 "agc", "bias_tee", "band_index", "fft_fps", "waterfall_speed",
                 "dsp_hpf", "dsp_hpf_hz", "dsp_lpf", "dsp_lpf_hz",
                 "dsp_notch", "dsp_notch_hz", "dsp_nr_level", "dsp_nb_level", "theme")

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
        self._func_last_used = time.monotonic()

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
    def mode_spec(self) -> config.ModeSpec:
        return config.MODE_SPECS[self.mode]

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
            self.tune_steps(detents)
        elif fn == "VOLUME":
            self.set_volume(self.volume + detents * config.VOLUME_STEP)
        elif fn == "SQUELCH":
            self.set_squelch(self.squelch_db + detents * config.SQUELCH_STEP_DB)
        elif fn == "GAIN":
            self.set_gain(self.gain_db + detents * config.GAIN_STEP_DB)

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

    # ------------------------------------------------------------------ memories
    def save_current_memory(self, name: str | None = None) -> Memory | None:
        if len(self.memories) >= config.MAX_MEMORIES:
            self.message.emit(f"Memory full ({config.MAX_MEMORIES})")
            return None
        mem = Memory(
            name=name or default_name(self.vfo_hz, self.mode),
            vfo_hz=self.vfo_hz,
            mode=self.mode,
            step_hz=self.step_hz,
        )
        self.memories.append(mem)
        self.memory_index = len(self.memories) - 1
        save_memories(self.memories)
        self.changed.emit("memories")
        self.message.emit(f"Saved {mem.name}")
        return mem

    def import_preset_memories(self, path=None) -> int:
        """Merge the bundled preset list into memories, skipping duplicates. Returns count added."""
        presets = load_memories(path or config.PRESET_MEMORIES_FILE)
        if not presets:
            self.message.emit("No presets found")
            return 0
        have = {(m.vfo_hz, m.mode) for m in self.memories}
        added = 0
        for mem in presets:
            if (mem.vfo_hz, mem.mode) in have or len(self.memories) >= config.MAX_MEMORIES:
                continue
            self.memories.append(mem)
            have.add((mem.vfo_hz, mem.mode))
            added += 1
        if added:
            save_memories(self.memories)
            self.changed.emit("memories")
        self.message.emit(f"Added {added} preset memories" if added else "Presets already loaded")
        return added

    def delete_memory(self, index: int) -> None:
        if not self.memories:
            return
        index %= len(self.memories)
        removed = self.memories.pop(index)
        self.memory_index = min(self.memory_index, max(0, len(self.memories) - 1))
        save_memories(self.memories)
        self.changed.emit("memories")
        self.message.emit(f"Deleted {removed.name}")

    def recall_memory(self, index: int) -> None:
        if not self.memories:
            self.message.emit("No memories")
            return
        index %= len(self.memories)
        mem = self.memories[index]
        self.memory_index = index
        self.set_mode(mem.mode)
        self.set_step_hz(mem.step_hz)
        self.set_vfo(mem.vfo_hz)
        self.message.emit(f"Memory {mem.name}")

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
        for key in self.PERSISTED:
            if key in data:
                setattr(self, key, data[key])
        self.band_index %= len(self.bands)
        self.step_index %= len(STEPS_HZ)
        if self.mode not in MODES:
            self.mode = MODES[0]
        self.vfo_hz = int(max(config.FREQ_MIN, min(config.FREQ_MAX, self.vfo_hz)))
        self.dsp_hpf = bool(self.dsp_hpf)
        self.dsp_lpf = bool(self.dsp_lpf)
        self.dsp_notch = bool(self.dsp_notch)
        self.dsp_hpf_hz = int(max(config.DSP_HPF_HZ_MIN, min(config.DSP_HPF_HZ_MAX, self.dsp_hpf_hz)))
        self.dsp_lpf_hz = int(max(config.DSP_LPF_HZ_MIN, min(config.DSP_LPF_HZ_MAX, self.dsp_lpf_hz)))
        self.dsp_notch_hz = int(max(config.DSP_NOTCH_HZ_MIN, min(config.DSP_NOTCH_HZ_MAX, self.dsp_notch_hz)))
        self.dsp_nr_level = int(max(0, min(config.DSP_LEVEL_MAX, self.dsp_nr_level)))
        self.dsp_nb_level = int(max(0, min(config.DSP_LEVEL_MAX, self.dsp_nb_level)))
        if self.theme not in config.THEME_NAMES:
            self.theme = config.DEFAULT_THEME
        for key in self.PERSISTED:
            self.changed.emit(key)


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
