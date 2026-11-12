"""Screen backlight via ``/sys/class/backlight`` (DSI panels; HDMI screens have none).

Writing ``brightness`` needs permission: ``install.sh`` adds a udev rule that gives the
``video`` group write access. Without it, :meth:`Backlight.set_percent` logs once and the
Settings row shows "no access".
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


class Backlight:
    def __init__(self, base: Path | None = None) -> None:
        base = base or config.BACKLIGHT_DIR
        self.path: Path | None = None
        self.max_raw = 0
        self.writable = False
        self._warned = False
        try:
            candidates = sorted(p for p in base.iterdir() if (p / "brightness").exists())
        except OSError:
            candidates = []
        for path in candidates:
            try:
                self.max_raw = int((path / "max_brightness").read_text().strip())
            except (OSError, ValueError):
                continue
            if self.max_raw > 0:
                self.path = path
                break
        if self.path is not None:
            self.writable = _can_write(self.path / "brightness")
            log.info("backlight %s (max %d, %s)", self.path.name, self.max_raw,
                     "writable" if self.writable else "read-only")

    @property
    def available(self) -> bool:
        return self.path is not None

    def status(self) -> str:
        if not self.available:
            return "no panel"
        return "" if self.writable else "no access"

    def set_percent(self, pct: float) -> None:
        if self.path is None or not self.writable:
            return
        raw = max(1, int(round(self.max_raw * max(0.0, min(100.0, pct)) / 100.0)))
        try:
            (self.path / "brightness").write_text(str(raw))
        except OSError as exc:
            if not self._warned:
                log.warning("cannot set backlight (%s) - run install.sh for the udev rule", exc)
                self._warned = True


def _can_write(path: Path) -> bool:
    try:
        with open(path, "r+"):
            return True
    except OSError:
        return False
