"""Secondary pages: band picker, mode picker, numeric keypad and settings.

Every page implements the small :class:`Page` protocol so the main window can route the
physical controls (four keys, encoder turn, encoder push) without knowing what is shown.
"""
from __future__ import annotations

import logging
import subprocess
import sys

from .. import config
from ..qt import Qt, QtWidgets, Signal
from ..state import RadioState, format_step
from . import style

log = logging.getLogger(__name__)


class Page(QtWidgets.QWidget):
    """Base class: soft-key labels plus hooks for the physical controls."""

    done = Signal()   # ask the main window to return to the main page

    def softkey_labels(self) -> list[str]:
        return ["", "", "", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 3:
            self.done.emit()

    def on_softkey_long(self, index: int) -> bool:
        return False

    def on_knob(self, detents: int) -> bool:
        """Return True if the page consumed the knob movement."""
        return False

    def on_encoder_push(self) -> bool:
        return False

    def on_shown(self) -> None:
        """Called every time the page becomes visible."""


# --------------------------------------------------------------------------- grid picker
def _band_freq_label(hz: int) -> str:
    """Compact frequency for a band tile: '4.625 MHz', '146.52 MHz', '1090 MHz'."""
    mhz = hz / 1e6
    if mhz >= 1000:
        return f"{mhz:.0f} MHz"
    digits = 2 if mhz >= 100 else 3
    return f"{mhz:.{digits}f}".rstrip("0").rstrip(".") + " MHz"


class _GridPicker(Page):
    """Scrollable grid of big buttons with one highlighted selection."""

    COLUMNS = 4

    def __init__(self, state: RadioState, title: str, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.items: list[str] = []
        self.buttons: list[QtWidgets.QPushButton] = []
        self.selected = 0

        # No scroll area: tiles expand to fill the page so nothing is ever cut off.
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(4)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("section")
        self.title.setFixedHeight(20)
        outer.addWidget(self.title)
        self.grid = QtWidgets.QGridLayout()
        self.grid.setSpacing(6)
        self.grid.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.grid, 1)
        outer.setAlignment(self.grid, Qt.AlignmentFlag.AlignTop)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)

    def set_items(self, items: list[str]) -> None:
        for btn in self.buttons:
            self.group.removeButton(btn)
            btn.deleteLater()
        self.buttons = []
        self.items = list(items)
        for i, text in enumerate(items):
            btn = QtWidgets.QPushButton(text)
            btn.setObjectName("tile")
            btn.setCheckable(True)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            btn.setMinimumHeight(44)
            btn.setMaximumHeight(110)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, i=i: self._activate(i))
            self.group.addButton(btn, i)
            self.grid.addWidget(btn, i // self.COLUMNS, i % self.COLUMNS)
            self.buttons.append(btn)
        for c in range(self.COLUMNS):
            self.grid.setColumnStretch(c, 1)
        rows = (len(items) + self.COLUMNS - 1) // self.COLUMNS
        for r in range(rows):
            self.grid.setRowStretch(r, 1)
        self._highlight()

    def _highlight(self) -> None:
        if not self.buttons:
            return
        self.selected %= len(self.buttons)
        self.buttons[self.selected].setChecked(True)

    def _activate(self, index: int) -> None:
        self.selected = index
        self._highlight()
        self.activate(index)
        self.done.emit()

    def activate(self, index: int) -> None:  # override
        raise NotImplementedError

    def move_selection(self, delta: int) -> None:
        if self.buttons:
            self.selected = (self.selected + delta) % len(self.buttons)
            self._highlight()

    def softkey_labels(self) -> list[str]:
        return ["UP", "DOWN", "SELECT", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            self.move_selection(-self.COLUMNS)
        elif index == 1:
            self.move_selection(+self.COLUMNS)
        elif index == 2:
            self._activate(self.selected)
        else:
            self.done.emit()

    def on_knob(self, detents: int) -> bool:
        self.move_selection(detents)
        return True

    def on_encoder_push(self) -> bool:
        self._activate(self.selected)
        return True


class BandPicker(_GridPicker):
    COLUMNS = 5

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(state, "BAND", parent)
        self.set_items([f"{b.name}\n{_band_freq_label(b.default_hz)}" for b in state.bands])

    def on_shown(self) -> None:
        self.selected = self.state.band_index
        self._highlight()

    def activate(self, index: int) -> None:
        self.state.select_band(index)


class ModePicker(_GridPicker):
    COLUMNS = 3

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(state, "MODE", parent)
        self.set_items([f"{m}\n{format_step(int(config.MODE_SPECS[m].bandwidth_hz))}" for m in config.MODES])

    def on_shown(self) -> None:
        self.selected = config.MODES.index(self.state.mode)
        self._highlight()

    def activate(self, index: int) -> None:
        self.state.set_mode(config.MODES[index])
        self.state.message.emit(f"Mode {self.state.mode}")


# --------------------------------------------------------------------------- keypad
class Keypad(Page):
    """Direct frequency entry. Type digits, then MHz or kHz to commit."""

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.entry = ""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(60, 4, 60, 4)
        layout.setSpacing(6)
        self.display = QtWidgets.QLabel("")
        self.display.setObjectName("entry")
        self.display.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.display.setMinimumHeight(60)
        layout.addWidget(self.display)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(6)
        keys = [("7", 0, 0), ("8", 0, 1), ("9", 0, 2), ("<-", 0, 3),
                ("4", 1, 0), ("5", 1, 1), ("6", 1, 2), ("CLR", 1, 3),
                ("1", 2, 0), ("2", 2, 1), ("3", 2, 2), ("kHz", 2, 3),
                ("0", 3, 0), (".", 3, 1), ("00", 3, 2), ("MHz", 3, 3)]
        for text, r, c in keys:
            btn = QtWidgets.QPushButton(text)
            btn.setObjectName("keypad")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, t=text: self.key(t))
            grid.addWidget(btn, r, c)
        layout.addLayout(grid, 1)
        self._refresh()

    def on_shown(self) -> None:
        self.entry = ""
        self._refresh()

    def _refresh(self) -> None:
        self.display.setText(self.entry if self.entry else "enter frequency")

    def key(self, text: str) -> None:
        if text == "<-":
            self.entry = self.entry[:-1]
        elif text == "CLR":
            self.entry = ""
        elif text in ("kHz", "MHz"):
            self.commit(1e3 if text == "kHz" else 1e6)
            return
        elif text == ".":
            if "." not in self.entry:
                self.entry += "." if self.entry else "0."
        else:
            if len(self.entry.replace(".", "")) < 10:
                self.entry += text
        self._refresh()

    def commit(self, multiplier: float) -> None:
        try:
            value = float(self.entry) * multiplier
        except ValueError:
            self.state.message.emit("Nothing entered")
            return
        if not config.FREQ_MIN <= value <= config.FREQ_MAX:
            self.state.message.emit("Out of range (0.5 - 1766 MHz)")
            return
        self.state.set_vfo(value)
        self.done.emit()

    def softkey_labels(self) -> list[str]:
        return ["MHz", "kHz", "CLR", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            self.commit(1e6)
        elif index == 1:
            self.commit(1e3)
        elif index == 2:
            self.key("CLR")
        else:
            self.done.emit()

    def on_knob(self, detents: int) -> bool:
        return True   # swallow; knob does nothing while typing

    def on_encoder_push(self) -> bool:
        self.commit(1e6)
        return True


# --------------------------------------------------------------------------- memories
def _mem_freq_label(hz: int) -> str:
    """'4.625 MHz', '146.52 MHz', '462.5625 MHz' - only as many decimals as needed."""
    mhz = hz / 1e6
    text = f"{mhz:.6f}".rstrip("0")
    whole, _, frac = text.partition(".")
    min_dec = 1 if mhz >= 1000 else 2 if mhz >= 100 else 3
    frac = frac.ljust(min_dec, "0")
    return f"{whole}.{frac} MHz"


class MemoryCard(QtWidgets.QFrame):
    """One memory: name on top, frequency and mode below. Tap to select / tune."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setProperty("selected", False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        self.name = QtWidgets.QLabel("")
        self.name.setObjectName("name")
        self.badge = QtWidgets.QLabel("")
        self.badge.setObjectName("badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.name, 1)
        top.addWidget(self.badge, 0)
        layout.addLayout(top)
        self.detail = QtWidgets.QLabel("")
        self.detail.setObjectName("detail")
        layout.addWidget(self.detail)

    def set_memory(self, name: str, detail: str, badge: str) -> None:
        self.name.setText(name)
        self.detail.setText(detail)
        self.badge.setText(badge)

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            for w in (self, self.name, self.detail, self.badge):
                style.repolish(w)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class MemoriesPage(Page):
    """Paged grid of memory cards. Tap to select, tap again (or TUNE) to recall."""

    COLUMNS = 3
    ROWS = 4
    PER_PAGE = COLUMNS * ROWS

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.selected = 0
        self.page = 0

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        self.title = QtWidgets.QLabel("MEMORIES")
        self.title.setObjectName("section")
        header.addWidget(self.title, 1)
        self.prev_btn = QtWidgets.QPushButton("\u25c0")
        self.next_btn = QtWidgets.QPushButton("\u25b6")
        self.page_lbl = QtWidgets.QLabel("")
        self.page_lbl.setObjectName("section")
        self.page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_lbl.setFixedWidth(48)
        for btn, delta in ((self.prev_btn, -1), (self.next_btn, +1)):
            btn.setObjectName("small")
            btn.setFixedSize(44, 26)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, d=delta: self._turn_page(d))
        header.addWidget(self.prev_btn)
        header.addWidget(self.page_lbl)
        header.addWidget(self.next_btn)
        layout.addLayout(header)

        self.empty = QtWidgets.QLabel("No saved frequencies yet.\n\nPress SAVE to store the current VFO,\n"
                                      "or load the HF presets from Settings.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("hint")
        layout.addWidget(self.empty, 1)

        self.grid_host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.cards: list[MemoryCard] = []
        for i in range(self.PER_PAGE):
            card = MemoryCard()
            card.clicked.connect(lambda i=i: self._tap(i))
            grid.addWidget(card, i // self.COLUMNS, i % self.COLUMNS)
            self.cards.append(card)
        for c in range(self.COLUMNS):
            grid.setColumnStretch(c, 1)
        for r in range(self.ROWS):
            grid.setRowStretch(r, 1)
        layout.addWidget(self.grid_host, 1)
        state.changed.connect(self._on_changed)

    # ------------------------------------------------------------------ helpers
    def _on_changed(self, field: str) -> None:
        if field == "memories" and self.isVisible():
            self.refresh()

    def on_shown(self) -> None:
        self.selected = min(self.state.memory_index, max(0, len(self.state.memories) - 1))
        self.page = self.selected // self.PER_PAGE
        self.refresh()

    @property
    def _pages(self) -> int:
        return max(1, (len(self.state.memories) + self.PER_PAGE - 1) // self.PER_PAGE)

    def refresh(self) -> None:
        memories = self.state.memories
        n = len(memories)
        self.empty.setVisible(n == 0)
        self.grid_host.setVisible(n > 0)
        self.title.setText(f"MEMORIES  {n}/{config.MAX_MEMORIES}")
        if n:
            self.selected %= n
            self.page = min(self.page, self._pages - 1)
            if not (self.page * self.PER_PAGE <= self.selected < (self.page + 1) * self.PER_PAGE):
                self.page = self.selected // self.PER_PAGE
        pages = self._pages
        self.page_lbl.setText(f"{self.page + 1}/{pages}")
        self.prev_btn.setVisible(pages > 1)
        self.next_btn.setVisible(pages > 1)
        self.page_lbl.setVisible(pages > 1)
        start = self.page * self.PER_PAGE
        tuned = (self.state.vfo_hz, self.state.mode)
        for i, card in enumerate(self.cards):
            idx = start + i
            if idx < n:
                mem = memories[idx]
                badge = "\u25cf" if (mem.vfo_hz, mem.mode) == tuned else ""
                card.set_memory(mem.name, f"{_mem_freq_label(mem.vfo_hz)}  \u00b7  {mem.mode}", badge)
                card.set_selected(idx == self.selected)
                card.setVisible(True)
            else:
                card.setVisible(False)

    def _turn_page(self, delta: int) -> None:
        if self._pages <= 1:
            return
        self.page = (self.page + delta) % self._pages
        self.selected = min(self.page * self.PER_PAGE, len(self.state.memories) - 1)
        self.refresh()

    def _tap(self, card_index: int) -> None:
        idx = self.page * self.PER_PAGE + card_index
        if idx >= len(self.state.memories):
            return
        if idx == self.selected:
            self._activate(idx)
            return
        self.selected = idx
        self.refresh()

    def _activate(self, index: int) -> None:
        self.selected = index
        self.state.recall_memory(index)
        self.done.emit()

    # ------------------------------------------------------------------ controls
    def softkey_labels(self) -> list[str]:
        return ["SAVE", "DELETE", "TUNE", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            if self.state.save_current_memory():
                self.selected = len(self.state.memories) - 1
                self.page = self.selected // self.PER_PAGE
            self.refresh()
        elif index == 1:
            if self.state.memories:
                self.state.delete_memory(self.selected)
                self.refresh()
        elif index == 2:
            if self.state.memories:
                self._activate(self.selected)
            else:
                self.state.message.emit("No memories")
        else:
            self.done.emit()

    def on_knob(self, detents: int) -> bool:
        n = len(self.state.memories)
        if n:
            self.selected = (self.selected + detents) % n
            self.refresh()
        return True

    def on_encoder_push(self) -> bool:
        if self.state.memories:
            self._activate(self.selected)
        return True


# --------------------------------------------------------------------------- settings
class SettingRow(QtWidgets.QFrame):
    """One settings line: label on the left, value on the right, tap to select/activate."""

    clicked = Signal()

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.setFixedHeight(34)
        self.setProperty("selected", False)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)
        self.label = QtWidgets.QLabel(label)
        self.value = QtWidgets.QLabel("")
        self.value.setObjectName("value")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.value, 0)

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            for w in (self, self.label, self.value):
                style.repolish(w)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class SettingsPage(Page):
    """Two columns of grouped rows. Tap a row to select it, tap again (or CHANGE) to
    toggle/activate; the knob steps the selected value."""

    quit_requested = Signal()

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.selected = 0
        s = state
        # (label, getter, adjuster(delta)); adjuster(0) means "activate / toggle".
        radio = [
            ("Tuner AGC", lambda: "ON" if s.agc else "OFF", lambda d: s.set_agc(not s.agc)),
            ("RF gain", lambda: f"{s.gain_db:.0f} dB" + (" (AGC)" if s.agc else ""),
             lambda d: s.set_gain(s.gain_db + (d or 1) * config.GAIN_STEP_DB)),
            ("Squelch", lambda: "open" if s.squelch_db <= config.SQUELCH_MIN_DB else f"{s.squelch_db:.0f} dB",
             lambda d: s.set_squelch(config.SQUELCH_MIN_DB if d == 0 and s.squelch_db > config.SQUELCH_MIN_DB
                                     else s.squelch_db + (d or 30) * config.SQUELCH_STEP_DB)),
            ("Volume", lambda: f"{s.volume} %",
             lambda d: s.set_volume(s.volume + (d or 5) * config.VOLUME_STEP)),
            ("Bias tee (4.5 V)", lambda: "ON" if s.bias_tee else "OFF",
             lambda d: s.set_bias_tee(not s.bias_tee)),
            ("Record audio", lambda: "REC" if s.recording else "OFF",
             lambda d: s.toggle_recording()),
        ]
        dsp = [
            ("Noise reduction", lambda: "OFF" if s.dsp_nr_level <= 0 else f"{s.dsp_nr_level}", self._adjust_nr),
            ("Noise blanker", lambda: "OFF" if s.dsp_nb_level <= 0 else f"{s.dsp_nb_level}", self._adjust_nb),
            ("Notch", lambda: "OFF" if not s.dsp_notch else f"{s.dsp_notch_hz} Hz", self._adjust_notch),
            ("High-pass", lambda: "OFF" if not s.dsp_hpf else f"{s.dsp_hpf_hz} Hz", self._adjust_hpf),
            ("Low-pass", lambda: "OFF" if not s.dsp_lpf else f"{s.dsp_lpf_hz} Hz", self._adjust_lpf),
        ]
        display = [
            ("Theme", lambda: s.theme, lambda d: s.cycle_theme(d or 1)),
            ("Spectrum rate", lambda: f"{s.fft_fps} fps",
             lambda d: s.set_fft_fps(s.fft_fps + 5 * (d or 1))),
            ("Waterfall speed", lambda: f"{s.waterfall_speed} row/frame",
             lambda d: s.set_waterfall_speed((s.waterfall_speed + (d or 1) - 1) % 4 + 1)),
        ]
        system = [
            ("HF conditions panel", lambda: s.hf_panel, lambda d: s.cycle_hf_panel(d or 1)),
            ("Load HF presets", lambda: "", lambda d: s.import_preset_memories() if d == 0 else None),
            ("Exit PiSDR", lambda: "", lambda d: self.quit_requested.emit() if d == 0 else None),
            ("Shut down Pi", lambda: "", lambda d: self._shutdown() if d == 0 else None),
        ]
        # Column layout: left = RADIO + DISPLAY, right = AUDIO DSP + SYSTEM.
        self.sections = [("RADIO", radio), ("DISPLAY", display), ("AUDIO DSP", dsp), ("SYSTEM", system)]
        self.rows: list[tuple[str, callable, callable]] = []
        self.widgets: list[SettingRow] = []

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(10)
        columns = (QtWidgets.QVBoxLayout(), QtWidgets.QVBoxLayout())
        for col in columns:
            col.setSpacing(3)
            col.setContentsMargins(0, 0, 0, 0)
            outer.addLayout(col, 1)
        for si, (title, items) in enumerate(self.sections):
            col = columns[0] if si < 2 else columns[1]
            head = QtWidgets.QLabel(title)
            head.setObjectName("section")
            head.setFixedHeight(20)
            col.addWidget(head)
            for label, getter, adjuster in items:
                index = len(self.rows)
                self.rows.append((label, getter, adjuster))
                row = SettingRow(label)
                row.clicked.connect(lambda i=index: self._tap(i))
                col.addWidget(row)
                self.widgets.append(row)
            col.addSpacing(4)
        for col in columns:
            col.addStretch(1)
        self._no_value = {i for i, (_, g, _a) in enumerate(self.rows) if g() == ""}
        state.changed.connect(lambda _f: self.refresh())
        self.refresh()

    def _adjust_nr(self, d: int) -> None:
        if d == 0:
            self.state.set_dsp_nr_level(0 if self.state.dsp_nr_level else 5)
        else:
            self.state.set_dsp_nr_level(self.state.dsp_nr_level + d)

    def _adjust_nb(self, d: int) -> None:
        if d == 0:
            self.state.set_dsp_nb_level(0 if self.state.dsp_nb_level else 5)
        else:
            self.state.set_dsp_nb_level(self.state.dsp_nb_level + d)

    def _adjust_notch(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_notch()
        elif self.state.dsp_notch:
            self.state.set_dsp_notch_hz(self.state.dsp_notch_hz + d * 50)
        else:
            self.state.set_dsp_notch(True)

    def _adjust_hpf(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_hpf()
        elif self.state.dsp_hpf:
            self.state.set_dsp_hpf_hz(self.state.dsp_hpf_hz + d * 25)
        else:
            self.state.set_dsp_hpf(True)

    def _adjust_lpf(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_lpf()
        elif self.state.dsp_lpf:
            self.state.set_dsp_lpf_hz(self.state.dsp_lpf_hz + d * 250)
        else:
            self.state.set_dsp_lpf(True)

    def refresh(self) -> None:
        for i, (_, getter, _a) in enumerate(self.rows):
            self.widgets[i].value.setText(getter())
            self.widgets[i].set_selected(i == self.selected)

    def _tap(self, index: int) -> None:
        if self.selected == index or index in self._no_value:
            self.selected = index
            self.rows[index][2](0)
        self.selected = index
        self.refresh()

    def _shutdown(self) -> None:
        self.state.save()
        self.state.message.emit("Shutting down...")
        if sys.platform.startswith("linux"):
            subprocess.Popen(["systemctl", "poweroff"])
        else:
            log.info("Shutdown requested (ignored on %s)", sys.platform)

    def softkey_labels(self) -> list[str]:
        return ["UP", "DOWN", "CHANGE", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            self.selected = (self.selected - 1) % len(self.rows)
        elif index == 1:
            self.selected = (self.selected + 1) % len(self.rows)
        elif index == 2:
            self.rows[self.selected][2](0)
        else:
            self.done.emit()
            return
        self.refresh()

    def on_knob(self, detents: int) -> bool:
        # Rows without a value (Exit / Shutdown) have nothing to step: knob moves selection.
        if self.selected in self._no_value:
            self.selected = (self.selected + detents) % len(self.rows)
        else:
            self.rows[self.selected][2](detents)
        self.refresh()
        return True

    def on_encoder_push(self) -> bool:
        self.rows[self.selected][2](0)
        self.refresh()
        return True
