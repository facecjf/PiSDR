"""Secondary pages: band picker, mode picker, numeric keypad and memories.

Every page implements the small :class:`Page` protocol so the main window can route the
physical controls (four keys, encoder turn, encoder push) without knowing what is shown.
"""
from __future__ import annotations

import logging
import time

from .. import config
from ..qt import Qt, QtWidgets, Signal
from ..state import RadioState, format_step
from . import style

log = logging.getLogger(__name__)


class Page(QtWidgets.QWidget):
    """Base class: soft-key labels plus hooks for the physical controls."""

    done = Signal()              # leave: go to ``return_to`` if set, else the main page
    done_main = Signal()         # leave straight to the main page
    softkeys_changed = Signal()  # ask the main window to re-read softkey_labels()
    return_to: str | None = None

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
        self.set_items([self._label(m) for m in config.MODES])

    def _label(self, mode: str) -> str:
        widths = config.FILTER_WIDTHS[mode]
        index = self.state.filter_index.get(mode, config.DEFAULT_FILTER_INDEX[mode])
        return f"{mode}\n{format_step(widths[index % len(widths)])}"

    def on_shown(self) -> None:
        for mode, btn in zip(config.MODES, self.buttons):
            btn.setText(self._label(mode))
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
    """Paged grid of memory cards for the selected group. Tap to select, tap again (or TUNE)
    to recall. Long-press TUNE makes the selected memory the priority channel; long-press
    DELETE locks it out of (or back into) memory scan."""

    COLUMNS = 3
    ROWS = 4
    PER_PAGE = COLUMNS * ROWS
    REFRESH_FIELDS = {"memories", "memory_group", "memory_scan_on_now", "priority", "lockouts",
                      "vfo_hz", "mode"}

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.selected = 0            # position in self.view
        self.page = 0
        self.view: list[int] = []    # indices into state.memories that pass the filters

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        self.title = QtWidgets.QLabel("MEMORIES")
        self.title.setObjectName("section")
        header.addWidget(self.title, 1)
        self.group_btn = QtWidgets.QPushButton("ALL")
        self.group_btn.setObjectName("small")
        self.group_btn.setFixedSize(170, 26)
        self.group_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.group_btn.clicked.connect(lambda: self._cycle_group(1))
        header.addWidget(self.group_btn)
        self.now_btn = QtWidgets.QPushButton("ON NOW")
        self.now_btn.setObjectName("small")
        self.now_btn.setCheckable(True)
        self.now_btn.setFixedSize(92, 26)
        self.now_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.now_btn.clicked.connect(self._toggle_on_now)
        header.addWidget(self.now_btn)
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

        self.empty = QtWidgets.QLabel("")
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
        if field in self.REFRESH_FIELDS and self.isVisible():
            self._rebuild_view(keep=True)
            self.refresh()

    def _rebuild_view(self, keep: bool = False) -> None:
        current = self.view[self.selected] if keep and 0 <= self.selected < len(self.view) else \
            self.state.memory_index
        self.view = self.state.memory_indices(on_now_only=self.state.memory_scan_on_now)
        self.selected = self.view.index(current) if current in self.view else 0

    def on_shown(self) -> None:
        self._rebuild_view()
        self.page = self.selected // self.PER_PAGE
        self.refresh()

    @property
    def _pages(self) -> int:
        return max(1, (len(self.view) + self.PER_PAGE - 1) // self.PER_PAGE)

    @property
    def _selected_index(self) -> int | None:
        return self.view[self.selected] if 0 <= self.selected < len(self.view) else None

    def _empty_text(self) -> str:
        s = self.state
        if not s.memories:
            return ("No saved frequencies yet.\n\nPress SAVE to store the current VFO,\n"
                    "or load the HF presets from Settings > SYSTEM.")
        where = "" if s.memory_group == config.MEMORY_GROUP_ALL else f" in {s.memory_group}"
        if s.memory_scan_on_now:
            return f"Nothing scheduled on now{where}.\n\nTap ON NOW to show every memory."
        return f"No memories{where}."

    def refresh(self) -> None:
        s = self.state
        n = len(self.view)
        self.empty.setVisible(n == 0)
        self.empty.setText(self._empty_text())
        self.grid_host.setVisible(n > 0)
        self.title.setText(f"MEMORIES  {len(s.memories)}/{config.MAX_MEMORIES}")
        self.group_btn.setText(f"{s.memory_group} \u25be")
        self.now_btn.setChecked(s.memory_scan_on_now)
        if n:
            self.selected %= n
            self.page = min(self.page, self._pages - 1)
            if not (self.page * self.PER_PAGE <= self.selected < (self.page + 1) * self.PER_PAGE):
                self.page = self.selected // self.PER_PAGE
        pages = self._pages
        self.page_lbl.setText(f"{self.page + 1}/{pages}")
        for w in (self.prev_btn, self.next_btn, self.page_lbl):
            w.setVisible(pages > 1)
        start = self.page * self.PER_PAGE
        tuned = (s.vfo_hz, s.mode)
        prio = (s.priority["vfo_hz"], s.priority["mode"]) if s.priority else None
        now = time.time()
        for i, card in enumerate(self.cards):
            pos = start + i
            if pos >= n:
                card.setVisible(False)
                continue
            mem = s.memories[self.view[pos]]
            badges = []
            if (mem.vfo_hz, mem.mode) == prio:
                badges.append("P")
            if mem.scheduled and mem.on_now(now):
                badges.append("ON")
            if s.is_locked_out(mem.vfo_hz):
                badges.append("\u2298")
            if (mem.vfo_hz, mem.mode) == tuned:
                badges.append("\u25cf")
            detail = f"{_mem_freq_label(mem.vfo_hz)}  \u00b7  {mem.mode}"
            if mem.utc:
                detail += f"  \u00b7  {mem.utc}Z"
            card.set_memory(mem.name, detail, " ".join(badges))
            card.set_selected(pos == self.selected)
            card.setVisible(True)

    def _cycle_group(self, delta: int) -> None:
        self.state.cycle_memory_group(delta)
        self.selected = 0
        self.page = 0
        self._rebuild_view()
        self.refresh()

    def _toggle_on_now(self) -> None:
        self.state.set_memory_scan_on_now(not self.state.memory_scan_on_now)

    def _turn_page(self, delta: int) -> None:
        if self._pages <= 1:
            return
        self.page = (self.page + delta) % self._pages
        self.selected = min(self.page * self.PER_PAGE, len(self.view) - 1)
        self.refresh()

    def _tap(self, card_index: int) -> None:
        pos = self.page * self.PER_PAGE + card_index
        if pos >= len(self.view):
            return
        if pos == self.selected:
            self._activate()
            return
        self.selected = pos
        self.refresh()

    def _activate(self) -> None:
        index = self._selected_index
        if index is None:
            self.state.message.emit("No memories")
            return
        self.state.recall_memory(index)
        self.done.emit()

    # ------------------------------------------------------------------ controls
    def softkey_labels(self) -> list[str]:
        return ["SAVE", "DELETE", "TUNE", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            if self.state.save_current_memory():
                self.state.memory_index = len(self.state.memories) - 1
                self._rebuild_view()
                self.page = self.selected // self.PER_PAGE
            self.refresh()
        elif index == 1:
            target = self._selected_index
            if target is not None:
                self.state.delete_memory(target)
                self._rebuild_view()
                self.selected = min(self.selected, max(0, len(self.view) - 1))
                self.refresh()
        elif index == 2:
            self._activate()
        else:
            self.done.emit()

    def on_softkey_long(self, index: int) -> bool:
        target = self._selected_index
        if target is None or index not in (1, 2):
            return index in (0, 1, 2)
        mem = self.state.memories[target]
        if index == 2:
            if self.state.priority and self.state.priority["vfo_hz"] == mem.vfo_hz \
                    and self.state.priority["mode"] == mem.mode:
                self.state.set_priority(None)
                self.state.message.emit("Priority off")
            else:
                self.state.set_priority({"name": mem.name, "vfo_hz": mem.vfo_hz,
                                         "mode": mem.mode, "step_hz": mem.step_hz})
                self.state.message.emit(f"Priority: {mem.name}")
        else:
            locked = self.state.toggle_lockout(mem.vfo_hz)
            self.state.message.emit(f"{mem.name} {'locked out' if locked else 'back in scan'}")
        return True

    def on_knob(self, detents: int) -> bool:
        n = len(self.view)
        if n:
            self.selected = (self.selected + detents) % n
            self.refresh()
        return True

    def on_encoder_push(self) -> bool:
        self._activate()
        return True
