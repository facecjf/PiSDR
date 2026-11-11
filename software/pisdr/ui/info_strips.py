"""Strips shown above the spectrum: RDS (station name / RadioText) and data-decoder output."""
from __future__ import annotations

from ..qt import Qt, QtWidgets
from ..state import RadioState
from . import style


class RdsPanel(QtWidgets.QWidget):
    """Programme Service name, programme type and RadioText while on broadcast FM."""

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setFixedHeight(52)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.setSpacing(12)
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(0)
        self.ps = QtWidgets.QLabel("")
        self.pty = QtWidgets.QLabel("")
        left.addWidget(self.ps)
        left.addWidget(self.pty)
        layout.addLayout(left, 0)
        self.rt = QtWidgets.QLabel("")
        self.rt.setWordWrap(True)
        self.rt.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.rt, 1)
        state.changed.connect(self._on_changed)
        self.retheme()
        self.refresh()

    def retheme(self) -> None:
        self.setStyleSheet(f"RdsPanel {{ background: {style.PANEL}; border-radius: 6px; }}")
        self.ps.setStyleSheet(
            f"background: transparent; font-family: 'DejaVu Sans Mono', monospace; font-size: 22px; "
            f"font-weight: bold; color: {style.ACCENT};")
        self.pty.setStyleSheet(f"background: transparent; color: {style.TEXT_DIM}; font-size: 12px;")
        self.rt.setStyleSheet(f"background: transparent; color: {style.TEXT}; font-size: 15px;")

    def _on_changed(self, field: str) -> None:
        if field in ("rds", "mode", "rds_enabled"):
            self.refresh()

    def refresh(self) -> None:
        s = self.state
        show = s.mode == "WFM" and s.rds_enabled
        self.setVisible(show)
        if not show:
            return
        self.ps.setText(s.rds_ps or "RDS")
        self.pty.setText(s.rds_pty or ("" if s.rds_ps else "waiting for data"))
        self.rt.setText(s.rds_rt)


class DecoderPanel(QtWidgets.QWidget):
    """Last two lines of multimon-ng output, the decoder name and a CLEAR button."""

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
        self.text.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.text, 1)
        side = QtWidgets.QVBoxLayout()
        side.setSpacing(2)
        self.name = QtWidgets.QLabel("")
        self.name.setAlignment(Qt.AlignmentFlag.AlignRight)
        side.addWidget(self.name)
        self.clear_btn = QtWidgets.QPushButton("CLEAR")
        self.clear_btn.setObjectName("small")
        self.clear_btn.setFixedSize(84, 32)
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.clicked.connect(state.clear_decoder_lines)
        side.addWidget(self.clear_btn)
        layout.addLayout(side)
        state.changed.connect(self._on_changed)
        self.retheme()
        self.refresh()

    def retheme(self) -> None:
        self.setStyleSheet(f"DecoderPanel {{ background: {style.PANEL}; border-radius: 6px; }}")
        self.text.setStyleSheet(
            f"background: transparent; font-family: 'DejaVu Sans Mono', monospace; "
            f"font-size: 14px; color: {style.OK};")
        self.name.setStyleSheet(f"background: transparent; color: {style.TEXT_DIM}; font-size: 13px;")

    def _on_changed(self, field: str) -> None:
        if field in ("decoder", "decoder_lines"):
            self.refresh()

    def refresh(self) -> None:
        s = self.state
        show = s.decoder != "OFF"
        self.setVisible(show)
        if not show:
            return
        self.name.setText(f"{s.decoder} \u00b7 {len(s.decoder_lines)}")
        lines = s.decoder_lines[-2:]
        self.text.setText("\n".join(lines) if lines else f"listening for {s.decoder}\u2026")
