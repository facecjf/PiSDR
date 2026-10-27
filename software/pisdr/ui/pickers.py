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
class _GridPicker(Page):
    """Scrollable grid of big buttons with one highlighted selection."""

    COLUMNS = 4

    def __init__(self, state: RadioState, title: str, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.items: list[str] = []
        self.buttons: list[QtWidgets.QPushButton] = []
        self.selected = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("title")
        outer.addWidget(self.title)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(container)
        self.grid.setSpacing(6)
        self.scroll.setWidget(container)
        outer.addWidget(self.scroll, 1)
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
            btn.setCheckable(True)
            btn.setMinimumHeight(60)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, i=i: self._activate(i))
            self.group.addButton(btn, i)
            self.grid.addWidget(btn, i // self.COLUMNS, i % self.COLUMNS)
            self.buttons.append(btn)
        self._highlight()

    def _highlight(self) -> None:
        if not self.buttons:
            return
        self.selected %= len(self.buttons)
        self.buttons[self.selected].setChecked(True)
        self.scroll.ensureWidgetVisible(self.buttons[self.selected], 0, 40)

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
    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(state, "BAND", parent)
        self.set_items([f"{b.name}\n{b.default_hz / 1e6:.3f} MHz" for b in state.bands])

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


# --------------------------------------------------------------------------- settings
class SettingsPage(Page):
    """A list of rows; knob adjusts the highlighted row, CHANGE toggles/steps it."""

    quit_requested = Signal()

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.selected = 0
        # (label, getter, adjuster(delta)) - adjuster(0) means "activate"
        self.rows: list[tuple[str, callable, callable]] = [
            ("Bias tee (4.5 V on antenna)", lambda: "ON" if state.bias_tee else "OFF",
             lambda d: state.set_bias_tee(not state.bias_tee)),
            ("Tuner AGC", lambda: "ON" if state.agc else "OFF",
             lambda d: state.set_agc(not state.agc)),
            ("RF gain", lambda: f"{state.gain_db:.0f} dB",
             lambda d: state.set_gain(state.gain_db + (d or 1) * config.GAIN_STEP_DB)),
            ("Squelch", lambda: "open" if state.squelch_db <= config.SQUELCH_MIN_DB else f"{state.squelch_db:.0f} dB",
             lambda d: state.set_squelch(state.squelch_db + (d or 1) * config.SQUELCH_STEP_DB)),
            ("Volume", lambda: f"{state.volume} %",
             lambda d: state.set_volume(state.volume + (d or 1) * config.VOLUME_STEP)),
            ("Spectrum rate", lambda: f"{state.fft_fps} fps",
             lambda d: state.set_fft_fps(state.fft_fps + 5 * (d or 1))),
            ("Waterfall speed", lambda: f"{state.waterfall_speed} rows/frame",
             lambda d: state.set_waterfall_speed((state.waterfall_speed + (d or 1) - 1) % 4 + 1)),
            ("Exit PiSDR", lambda: "", lambda d: self.quit_requested.emit() if d == 0 else None),
            ("Shut down Raspberry Pi", lambda: "", lambda d: self._shutdown() if d == 0 else None),
        ]
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(3)
        title = QtWidgets.QLabel("SETTINGS")
        title.setObjectName("title")
        title.setFixedHeight(24)
        layout.addWidget(title)
        self.buttons: list[QtWidgets.QPushButton] = []
        self.group = QtWidgets.QButtonGroup(self)
        for i, _ in enumerate(self.rows):
            btn = QtWidgets.QPushButton("")
            btn.setCheckable(True)
            btn.setFixedHeight(36)
            btn.setStyleSheet("text-align: left; font-size: 17px; min-height: 30px;")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, i=i: self._tap(i))
            self.group.addButton(btn, i)
            layout.addWidget(btn)
            self.buttons.append(btn)
        layout.addStretch(1)
        state.changed.connect(lambda _f: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        for i, (label, getter, _) in enumerate(self.rows):
            value = getter()
            self.buttons[i].setText(f"{label:<32}{value:>14}" if value else label)
        self.buttons[self.selected].setChecked(True)

    def _tap(self, index: int) -> None:
        if self.selected == index:
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
        self.rows[self.selected][2](detents)
        self.refresh()
        return True

    def on_encoder_push(self) -> bool:
        self.rows[self.selected][2](0)
        self.refresh()
        return True
