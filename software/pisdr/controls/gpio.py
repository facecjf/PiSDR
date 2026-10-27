"""GPIO front-panel controls via gpiozero, marshalled onto the Qt thread with signals.

gpiozero fires its callbacks from its own threads. Emitting a Qt signal from there is safe:
the connection to a receiver living in the GUI thread is automatically queued, so the UI
only ever sees the events on its own thread.

Wiring (BCM numbers, see docs/01_hardware.md):

* EC11 module CLK -> ENCODER_A_GPIO, DT -> ENCODER_B_GPIO, SW -> ENCODER_SW_GPIO,
  ``+`` -> 3.3 V, GND -> GND.
* Keys K1..K4 -> KEY_GPIOS, other side to GND.

Encoder detents are coalesced: fast spins deliver one ``rotated(n)`` with ``n`` > 1 instead
of a burst of single steps, which keeps the UI responsive on the Pi.
"""
from __future__ import annotations

import logging
import threading

from .. import config
from ..qt import QObject, QTimer, Signal

log = logging.getLogger(__name__)


class GpioControls(QObject):
    rotated = Signal(int)            # +n clockwise, -n counter-clockwise
    encoder_pushed = Signal()        # short press on the knob
    encoder_held = Signal()          # long press on the knob
    key_pressed = Signal(int)        # 0..3, short press
    key_long_pressed = Signal(int)   # 0..3, held KEY_HOLD_S

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Imported here so the rest of the app runs on machines without gpiozero.
        from gpiozero import Button, RotaryEncoder

        self._lock = threading.Lock()
        self._pending = 0
        self._flush = QTimer(self)
        self._flush.setInterval(15)
        self._flush.timeout.connect(self._flush_rotation)
        self._flush.start()

        self.encoder = RotaryEncoder(config.ENCODER_A_GPIO, config.ENCODER_B_GPIO,
                                     max_steps=0, wrap=False, bounce_time=None)
        self.encoder.when_rotated_clockwise = lambda: self._rotate(+1)
        self.encoder.when_rotated_counter_clockwise = lambda: self._rotate(-1)

        self.encoder_sw = Button(config.ENCODER_SW_GPIO, pull_up=True,
                                 bounce_time=config.KEY_BOUNCE_S, hold_time=config.KEY_HOLD_S)
        self.encoder_sw.when_held = self._encoder_held
        self.encoder_sw.when_released = self._encoder_released
        self._encoder_was_held = False

        self.keys = []
        self._key_was_held = [False] * len(config.KEY_GPIOS)
        for i, pin in enumerate(config.KEY_GPIOS):
            btn = Button(pin, pull_up=True, bounce_time=config.KEY_BOUNCE_S, hold_time=config.KEY_HOLD_S)
            btn.when_held = lambda i=i: self._key_held(i)
            btn.when_released = lambda i=i: self._key_released(i)
            self.keys.append(btn)
        log.info("GPIO: encoder A=%d B=%d SW=%d keys=%s", config.ENCODER_A_GPIO,
                 config.ENCODER_B_GPIO, config.ENCODER_SW_GPIO, config.KEY_GPIOS)

    # ------------------------------------------------------------------ encoder
    def _rotate(self, direction: int) -> None:
        with self._lock:
            self._pending += direction

    def _flush_rotation(self) -> None:
        with self._lock:
            n, self._pending = self._pending, 0
        if n:
            self.rotated.emit(n)

    def _encoder_held(self) -> None:
        self._encoder_was_held = True
        self.encoder_held.emit()

    def _encoder_released(self) -> None:
        if not self._encoder_was_held:
            self.encoder_pushed.emit()
        self._encoder_was_held = False

    # ------------------------------------------------------------------ keys
    def _key_held(self, index: int) -> None:
        self._key_was_held[index] = True
        self.key_long_pressed.emit(index)

    def _key_released(self, index: int) -> None:
        if not self._key_was_held[index]:
            self.key_pressed.emit(index)
        self._key_was_held[index] = False

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._flush.stop()
        for dev in [self.encoder, self.encoder_sw, *self.keys]:
            try:
                dev.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass


if __name__ == "__main__":
    # Wiring test: python3 -m pisdr.controls.gpio  (prints events for 60 s)
    import sys
    from ..qt import QtCore

    logging.basicConfig(level=logging.INFO)
    app = QtCore.QCoreApplication(sys.argv)
    ctl = GpioControls()
    ctl.rotated.connect(lambda n: print(f"knob {n:+d}"))
    ctl.encoder_pushed.connect(lambda: print("knob push"))
    ctl.encoder_held.connect(lambda: print("knob hold"))
    ctl.key_pressed.connect(lambda i: print(f"K{i + 1} press"))
    ctl.key_long_pressed.connect(lambda i: print(f"K{i + 1} hold"))
    print("Turn the knob and press keys; exits after 60 s")
    QtCore.QTimer.singleShot(60_000, app.quit)
    app.exec()
    ctl.close()
