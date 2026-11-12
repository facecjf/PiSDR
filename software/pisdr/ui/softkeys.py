"""Soft-key bar: four labels along the bottom edge, aligned above the four physical keys.

The bar spans the full 800 px active width in four equal cells so the physical key under
each cell (see ``cad/pisdr_params.scad``: ``key_centres_x``) lines up with its label. The
labels are also touch buttons, so the radio is fully usable before the keys are wired.
"""
from __future__ import annotations

from .. import config
from ..qt import Qt, QtCore, QtWidgets, Signal


class SoftKeyBar(QtWidgets.QWidget):
    pressed = Signal(int)        # key index 0..3, short tap
    long_pressed = Signal(int)   # key index 0..3, held for KEY_HOLD_S

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.buttons: list[QtWidgets.QPushButton] = []
        self._hold_timers: list[QtCore.QTimer] = []
        self._held = [False] * 4
        self._flashed: set[int] = set()
        # Owned by the bar, so a flash still pending when the window closes is dropped.
        self._flash_timer = QtCore.QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(120)
        self._flash_timer.timeout.connect(self._unflash)
        for i in range(4):
            btn = QtWidgets.QPushButton("")
            btn.setObjectName("softkey")
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.pressed.connect(lambda i=i: self._on_pressed(i))
            btn.released.connect(lambda i=i: self._on_released(i))
            layout.addWidget(btn, 1)
            self.buttons.append(btn)

            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(int(config.KEY_HOLD_S * 1000))
            timer.timeout.connect(lambda i=i: self._on_hold(i))
            self._hold_timers.append(timer)

    def set_labels(self, labels: list[str]) -> None:
        for btn, label in zip(self.buttons, labels):
            btn.setText(label)
            btn.setEnabled(bool(label))

    def flash(self, index: int) -> None:
        """Visual feedback when a physical key is pressed."""
        if 0 <= index < 4:
            self.buttons[index].setDown(True)
            self._flashed.add(index)
            self._flash_timer.start()

    def _unflash(self) -> None:
        for index in self._flashed:
            self.buttons[index].setDown(False)
        self._flashed.clear()

    # Touch handling mirrors the physical keys: short tap -> pressed, hold -> long_pressed.
    def _on_pressed(self, i: int) -> None:
        self._held[i] = False
        self._hold_timers[i].start()

    def _on_released(self, i: int) -> None:
        self._hold_timers[i].stop()
        if not self._held[i]:
            self.pressed.emit(i)

    def _on_hold(self, i: int) -> None:
        self._held[i] = True
        self.long_pressed.emit(i)
