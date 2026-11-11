"""multimon-ng subprocess: 22.05 kHz s16le mono on stdin, one decoded message per stdout line.

The backend owns one :class:`Multimon`. The GNU Radio feed block calls :meth:`write` from the
scheduler thread; writes are non-blocking and silently dropped if multimon-ng falls behind,
so a stuck decoder can never stall the radio audio.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading

from .. import config
from ..qt import QObject, Signal

log = logging.getLogger(__name__)


class Multimon(QObject):
    line = Signal(str)   # emitted from the reader thread; Qt queues it to the GUI thread

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._fd: int | None = None
        self.name = "OFF"

    @staticmethod
    def available() -> bool:
        return shutil.which("multimon-ng") is not None

    @property
    def running(self) -> bool:
        return self._proc is not None

    def start(self, name: str) -> str | None:
        """Start decoding *name* (a key of config.DECODERS). Returns an error text or None."""
        self.stop()
        args = config.DECODERS.get(name)
        if not args:
            return None
        exe = shutil.which("multimon-ng")
        if not exe:
            return "multimon-ng is not installed: sudo apt install multimon-ng"
        cmd = [exe, "-t", "raw", *args, "-"]
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL", *cmd]   # line-buffer its stdout so messages arrive promptly
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            return f"multimon-ng failed to start: {exc}"
        os.set_blocking(proc.stdin.fileno(), False)
        self._proc = proc
        self._fd = proc.stdin.fileno()
        self.name = name
        threading.Thread(target=self._read, args=(proc,), name="multimon-reader", daemon=True).start()
        log.info("multimon-ng started: %s", " ".join(cmd))
        return None

    def write(self, data: bytes) -> None:
        """Called from the GNU Radio thread with s16le samples at DECODER_RATE."""
        fd = self._fd
        if fd is None:
            return
        try:
            os.write(fd, data)
        except BlockingIOError:
            pass                       # decoder is behind; drop this chunk
        except OSError:
            self._fd = None            # multimon-ng exited

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").strip()
            if text and not text.startswith("Enabled demodulators"):
                self.line.emit(text)
        if self._proc is proc:
            log.warning("multimon-ng exited (code %s)", proc.poll())

    def stop(self) -> None:
        proc, self._proc, self._fd = self._proc, None, None
        self.name = "OFF"
        if proc is None:
            return
        # The radio thread may be mid-write with the old fd; close it a moment later so the
        # number cannot be reused by another file while a write is still in flight.
        threading.Timer(0.5, _reap, args=(proc,)).start()


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.close()
    except OSError:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
