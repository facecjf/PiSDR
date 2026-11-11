"""List pages opened from Settings: recordings browser, scan hit log, network mode."""
from __future__ import annotations

import logging
import re
import time
import wave
from pathlib import Path

from .. import config, sysinfo
from ..qt import Qt, QTimer, QtWidgets, Signal
from ..state import RadioState
from . import style
from .pickers import Page

log = logging.getLogger(__name__)

_REC_RE = re.compile(r"pisdr_(\d{8})_(\d{6})_(\d+)_([A-Z]+)\.wav$")


class ListRow(QtWidgets.QFrame):
    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.setFixedHeight(34)
        self.setProperty("selected", False)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(8)
        self.left = QtWidgets.QLabel("")
        self.right = QtWidgets.QLabel("")
        self.right.setObjectName("value")
        self.right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.left, 1)
        layout.addWidget(self.right, 0)

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            for w in (self, self.left, self.right):
                style.repolish(w)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class _ListPage(Page):
    """Paged list with a title, a page indicator and one selected row."""

    ROWS = 9
    return_to = "settings"

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.selected = 0
        self.items: list = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(3)
        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("section")
        self.title.setFixedHeight(22)
        self.page_lbl = QtWidgets.QLabel("")
        self.page_lbl.setObjectName("section")
        self.page_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.title, 1)
        header.addWidget(self.page_lbl, 0)
        layout.addLayout(header)
        self.empty = QtWidgets.QLabel("")
        self.empty.setObjectName("hint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty, 1)
        self.rows: list[ListRow] = []
        for i in range(self.ROWS):
            row = ListRow()
            row.clicked.connect(lambda i=i: self._tap(i))
            layout.addWidget(row)
            self.rows.append(row)
        layout.addStretch(1)

    # Subclasses: load_items() -> list, row_text(item) -> (left, right), activate(item)
    def load_items(self) -> list:
        return []

    def row_text(self, item) -> tuple[str, str]:
        return str(item), ""

    def activate(self, item) -> None:
        """Encoder push / second tap on a row."""

    def empty_text(self) -> str:
        return "Nothing here yet."

    @property
    def current(self):
        return self.items[self.selected] if 0 <= self.selected < len(self.items) else None

    def on_shown(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.items = self.load_items()
        self.selected = min(self.selected, max(0, len(self.items) - 1))
        self.refresh()

    def refresh(self) -> None:
        n = len(self.items)
        self.empty.setVisible(n == 0)
        self.empty.setText(self.empty_text())
        page = self.selected // self.ROWS
        pages = max(1, (n + self.ROWS - 1) // self.ROWS)
        self.page_lbl.setText(f"{n} \u00b7 page {page + 1}/{pages}" if n else "")
        for i, row in enumerate(self.rows):
            pos = page * self.ROWS + i
            if pos < n:
                left, right = self.row_text(self.items[pos])
                row.left.setText(left)
                row.right.setText(right)
                row.set_selected(pos == self.selected)
                row.setVisible(True)
            else:
                row.setVisible(False)

    def move(self, delta: int) -> None:
        if self.items:
            self.selected = (self.selected + delta) % len(self.items)
            self.refresh()

    def _tap(self, row_index: int) -> None:
        pos = (self.selected // self.ROWS) * self.ROWS + row_index
        if pos >= len(self.items):
            return
        if pos == self.selected:
            self.activate(self.items[pos])
        self.selected = pos
        self.refresh()

    def on_knob(self, detents: int) -> bool:
        self.move(detents)
        return True

    def on_encoder_push(self) -> bool:
        if self.current is not None:
            self.activate(self.current)
            self.refresh()
        return True


# --------------------------------------------------------------------------- recordings
def _recording_label(path: Path) -> str:
    m = _REC_RE.search(path.name)
    if not m:
        return path.name
    day, hms, hz, mode = m.groups()
    return f"{day[4:6]}-{day[6:8]} {hms[0:2]}:{hms[2:4]}   {int(hz) / 1e6:.4f} MHz {mode}"


def _recording_detail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with wave.open(str(path), "rb") as wav:
            seconds = int(wav.getnframes() / max(1, wav.getframerate()))
        return f"{seconds // 60}:{seconds % 60:02d}  {size / 1e6:.1f} MB"
    except (OSError, EOFError, wave.Error):
        return "?"


class RecordingsPage(_ListPage):
    """Recordings in ~/Recordings, newest first. PLAY plays through the radio audio."""

    def __init__(self, state: RadioState, backend, parent=None) -> None:
        super().__init__("RECORDINGS", parent)
        self.state = state
        self.backend = backend
        self._delete_armed = 0.0
        self._details: dict[Path, str] = {}
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field == "playing" and self.isVisible():
            self.refresh()
            self.softkeys_changed.emit()

    def load_items(self) -> list:
        try:
            files = list(config.RECORDINGS_DIR.glob("*.wav"))
        except OSError:
            files = []
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        self._details = {}
        return files

    def row_text(self, item: Path) -> tuple[str, str]:
        if item not in self._details:
            self._details[item] = _recording_detail(item)
        playing = self.state.playing == str(item)
        return ("\u25b6 " if playing else "") + _recording_label(item), self._details[item]

    def empty_text(self) -> str:
        return f"No recordings yet.\n\nRecordings are saved to {config.RECORDINGS_DIR}"

    def activate(self, item: Path) -> None:
        if self.state.playing == str(item):
            self.backend.stop_playback()
            return
        error = self.backend.play_file(str(item))
        if error:
            self.state.message.emit(error)

    def softkey_labels(self) -> list[str]:
        play = "STOP" if self.state.playing else "PLAY"
        delete = "CONFIRM" if time.monotonic() - self._delete_armed < 3.0 else "DELETE"
        return [play, delete, "NEXT", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            if self.state.playing:
                self.backend.stop_playback()
            elif self.current is not None:
                self.activate(self.current)
        elif index == 1:
            self._delete()
        elif index == 2:
            self.move(1)
        else:
            self.done.emit()
            return
        self.refresh()

    def _delete(self) -> None:
        item = self.current
        if item is None:
            return
        now = time.monotonic()
        if now - self._delete_armed >= 3.0:
            self._delete_armed = now
            self.state.message.emit(f"Press CONFIRM to delete {item.name}")
            QTimer.singleShot(3100, self.softkeys_changed.emit)
            return
        self._delete_armed = 0.0
        if self.state.playing == str(item):
            self.backend.stop_playback()
        try:
            item.unlink()
            self.state.message.emit(f"Deleted {item.name}")
        except OSError as exc:
            self.state.message.emit(f"Delete failed: {exc}")
        self.reload()

    def hideEvent(self, event) -> None:  # noqa: N802
        # Playback belongs to this page; leaving it returns to the live radio.
        self.backend.stop_playback()
        super().hideEvent(event)


# --------------------------------------------------------------------------- scan log
class ScanLogPage(_ListPage):
    """Scanner hits, newest first. TUNE goes there, LOCK adds a lockout."""

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__("SCAN LOG", parent)
        self.state = state
        self._clear_armed = 0.0

    def load_items(self) -> list:
        return list(self.state.scan_log)

    def row_text(self, hit: dict) -> tuple[str, str]:
        t = hit.get("t", 0)
        tm = time.gmtime(t) if self.state.clock_utc else time.localtime(t)
        stamp = time.strftime("%m-%d %H:%M", tm) + ("Z" if self.state.clock_utc else "")
        freq = f"{hit.get('vfo_hz', 0) / 1e6:.4f} {hit.get('mode', '')}"
        name = hit.get("name") or ""
        src = {"priority": " (P)", "memory": "", "band": ""}.get(hit.get("src", ""), "")
        left = f"{stamp}   {freq}" + (f"   {name}" if name else "") + src
        locked = " \u2298" if self.state.is_locked_out(hit.get("vfo_hz", 0)) else ""
        return left, f"{hit.get('dur_s', 0):.0f} s  {hit.get('peak_db', 0):.0f} dB{locked}"

    def empty_text(self) -> str:
        return "No scan hits yet.\n\nHits are logged whenever the scanner stops on a signal."

    def activate(self, hit: dict) -> None:
        mode = hit.get("mode", "AM")
        if mode in config.MODES:
            self.state.set_mode(mode)
        self.state.set_vfo(hit.get("vfo_hz", self.state.vfo_hz))
        self.state.message.emit(f"Tuned {hit.get('vfo_hz', 0) / 1e6:.4f} MHz")
        self.done_main.emit()

    def softkey_labels(self) -> list[str]:
        clear = "CONFIRM" if time.monotonic() - self._clear_armed < 3.0 else "CLEAR"
        return ["TUNE", "LOCK", clear, "BACK"]

    def on_softkey(self, index: int) -> None:
        hit = self.current
        if index == 0:
            if hit is not None:
                self.activate(hit)
            return
        if index == 1 and hit is not None:
            locked = self.state.toggle_lockout(hit.get("vfo_hz", 0))
            self.state.message.emit("Locked out" if locked else "Lockout removed")
        elif index == 2:
            now = time.monotonic()
            if now - self._clear_armed < 3.0:
                self._clear_armed = 0.0
                self.state.clear_scan_log()
                self.reload()
            else:
                self._clear_armed = now
                self.state.message.emit("Press CONFIRM to clear the scan log")
                QTimer.singleShot(3100, self.softkeys_changed.emit)
        elif index == 3:
            self.done.emit()
            return
        self.refresh()


# --------------------------------------------------------------------------- network mode
class NetworkPage(Page):
    """Shown while rtl_tcp owns the dongle. STOP gives it back to PiSDR."""

    stop_requested = Signal()

    def __init__(self, state: RadioState, backend, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.backend = backend
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(40, 16, 40, 16)
        layout.setSpacing(10)
        title = QtWidgets.QLabel("NETWORK MODE")
        title.setObjectName("title")
        layout.addWidget(title)
        self.address = QtWidgets.QLabel("")
        self.address.setObjectName("entry")
        self.address.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.address)
        self.body = QtWidgets.QLabel("")
        self.body.setObjectName("hint")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.body, 1)
        self._check = QTimer(self)
        self._check.setInterval(2000)
        self._check.timeout.connect(self._update)

    def on_shown(self) -> None:
        self._update()
        self._check.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._check.stop()
        super().hideEvent(event)

    def _update(self) -> None:
        ip = sysinfo.lan_address()
        self.address.setText(f"{ip} : {config.RTL_TCP_PORT}")
        alive = getattr(self.backend, "rtl_tcp_alive", True)
        state = "rtl_tcp is serving the dongle." if alive else \
            "rtl_tcp has stopped - press STOP to get the radio back."
        self.body.setText(
            f"{state}\n\nIn SDR++, SDR# or GQRX choose the RTL-TCP source and connect to "
            f"{ip} (or {sysinfo.hostname()}.local) port {config.RTL_TCP_PORT}.\n\n"
            "PiSDR's receiver, scanner, recorder and decoders are paused. "
            "Press STOP (or push the knob) to take the dongle back.")

    def softkey_labels(self) -> list[str]:
        return ["", "", "", "STOP"]

    def on_softkey(self, index: int) -> None:
        if index == 3:
            self.stop_requested.emit()

    def on_softkey_long(self, index: int) -> bool:
        return True

    def on_knob(self, detents: int) -> bool:
        return True

    def on_encoder_push(self) -> bool:
        self.stop_requested.emit()
        return True
