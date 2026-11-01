"""Decoded-Morse strip shown under the frequency display while in CW mode."""
from __future__ import annotations

from ..qt import Qt, QtWidgets
from ..state import RadioState
from . import style


class CwPanel(QtWidgets.QWidget):
    """Two lines of decoded text, the estimated WPM, and a CLEAR button."""

    LINE_CHARS = 48

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setFixedHeight(64)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 6, 4)
        layout.setSpacing(8)

        self.text = QtWidgets.QLabel("")
        self.text.setWordWrap(False)
        self.text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.text, 1)

        side = QtWidgets.QVBoxLayout()
        side.setSpacing(2)
        self.wpm = QtWidgets.QLabel("CW --")
        self.wpm.setAlignment(Qt.AlignmentFlag.AlignRight)
        side.addWidget(self.wpm)
        self.clear_btn = QtWidgets.QPushButton("CLEAR")
        self.clear_btn.setObjectName("small")
        self.clear_btn.setFixedSize(84, 32)
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.clicked.connect(state.clear_cw_text)
        side.addWidget(self.clear_btn)
        layout.addLayout(side)

        state.changed.connect(self._on_changed)
        self.retheme()
        self.refresh()

    def retheme(self) -> None:
        self.setStyleSheet(f"CwPanel {{ background: {style.PANEL}; border-radius: 6px; }}")
        self.text.setStyleSheet(
            f"background: transparent; font-family: 'DejaVu Sans Mono', 'Consolas', monospace; "
            f"font-size: 20px; color: {style.OK};")
        self.wpm.setStyleSheet(f"background: transparent; color: {style.TEXT_DIM}; font-size: 13px;")

    def _on_changed(self, field: str) -> None:
        if field in ("cw_text", "cw_wpm", "mode"):
            self.refresh()

    def refresh(self) -> None:
        is_cw = self.state.mode == "CW"
        self.setVisible(is_cw)
        if not is_cw:
            return
        text = self.state.cw_text
        # Show the last two "lines" of the buffer.
        tail = text[-2 * self.LINE_CHARS:]
        if len(tail) > self.LINE_CHARS:
            tail = tail[:-self.LINE_CHARS] + "\n" + tail[-self.LINE_CHARS:]
        self.text.setText(tail if tail else "listening for CW…")
        wpm = self.state.cw_wpm
        self.wpm.setText(f"CW {wpm:.0f} WPM" if wpm > 0 else "CW --")
