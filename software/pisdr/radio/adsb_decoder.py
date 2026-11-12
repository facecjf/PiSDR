"""Runs a dump1090-family decoder on the dongle and reads the aircraft list it writes.

Works with dump1090-fa, dump1090-mutability and readsb: all of them take ``--write-json
<dir>`` and rewrite ``<dir>/aircraft.json`` every second. No network ports are opened. The
folder is on a RAM disk when there is one, so the SD card is not written every second.

The caller must have closed the dongle before :meth:`AdsbDecoder.start` and may reopen it
after :meth:`AdsbDecoder.stop`.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config
from ..adsb import Aircraft, parse_aircraft_json

log = logging.getLogger(__name__)

INSTALL_HINT = "run software/install.sh on the Pi to build it"
_START_WAIT_S = 1.0          # a decoder that cannot claim the dongle exits well inside this
_HELP: dict[str, str] = {}


def find_program() -> str | None:
    """Full path of the first ADS-B decoder on PATH, or None."""
    for name in config.ADSB_PROGRAMS:
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def help_text(exe: str) -> str:
    """The decoder's ``--help`` output; the variants differ and this is how they are told apart."""
    if exe not in _HELP:
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=5)
            _HELP[exe] = out.stdout + out.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("could not run %s --help: %s", exe, exc)
            _HELP[exe] = ""
    return _HELP[exe]


def problem_with(exe: str | None) -> str | None:
    """Why *exe* cannot be used, in words for the screen, or None if it can."""
    if not exe:
        return f"No ADS-B decoder (dump1090) found - {INSTALL_HINT}"
    text = help_text(exe)
    if not text:
        return f"{Path(exe).name} does not run - {INSTALL_HINT}"
    if "write-json" not in text:
        return f"{Path(exe).name} is too old (no --write-json option) - {INSTALL_HINT}"
    return None


def bias_tee_option(exe: str) -> str | None:
    """The decoder's own switch for the bias tee (readsb has one; dump1090 does not)."""
    found = re.search(r"--enable-[\w-]*bias[\w-]*", help_text(exe))
    return found.group(0) if found else None


def build_command(exe: str, json_dir: str, ppm: int = 0, bias_tee: bool = False,
                  position: tuple[float, float] | None = None) -> list[str]:
    """Command line for *exe*, using only options its ``--help`` lists."""
    text = help_text(exe)
    cmd = [exe]
    if "device-type" in text:
        cmd += ["--device-type", "rtlsdr"]         # has to come before the rtlsdr-specific options
    cmd += ["--quiet", "--write-json", json_dir, "--write-json-every", "1"]
    if ppm and "--ppm" in text:
        cmd += ["--ppm", str(int(ppm))]
    if bias_tee and bias_tee_option(exe):
        cmd += [bias_tee_option(exe)]
    if position is not None and "--lat" in text:   # helps it place aircraft heard only once
        cmd += ["--lat", f"{position[0]:.4f}", "--lon", f"{position[1]:.4f}"]
    return cmd


def _scratch_base() -> str | None:
    """A RAM-backed folder if the system has one (None = the normal temp folder)."""
    for base in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return base
    return None


class AdsbDecoder:
    """One decoder process and the folder it writes into."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._dir: Path | None = None
        self._log = None
        self._last: tuple[list[Aircraft], int] = ([], 0)
        self.program = ""

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ppm: int = 0, bias_tee: bool = False,
              position: tuple[float, float] | None = None) -> str | None:
        """Start decoding. Returns an error text, or None once the decoder holds the dongle."""
        self.stop()
        exe = find_program()
        problem = problem_with(exe)
        if problem:
            return problem
        self.program = Path(exe).name
        try:
            self._dir = Path(tempfile.mkdtemp(prefix="pisdr-adsb-", dir=_scratch_base()))
            cmd = build_command(exe, str(self._dir), ppm, bias_tee, position)
            if bias_tee and not bias_tee_option(exe):
                self._bias_tee_on()
            self._log = open(self._dir / "decoder.log", "wb")
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=self._log)
        except OSError as exc:
            self.stop()
            return f"{self.program} failed to start: {exc}"
        try:
            self._proc.wait(timeout=_START_WAIT_S)
        except subprocess.TimeoutExpired:
            log.info("ADS-B decoder running: %s", " ".join(cmd))
            return None
        reason = self.exit_reason()
        self.stop()
        return f"{self.program} stopped: {reason}"

    def _bias_tee_on(self) -> None:
        """The decoder has no bias-tee option: switch it on with rtl_biast first (it stays on)."""
        tool = shutil.which("rtl_biast")
        if not tool:
            log.warning("bias tee is on in settings but neither %s nor rtl_biast can switch it on", self.program)
            return
        try:
            subprocess.run([tool, "-b", "1"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("rtl_biast failed: %s", exc)

    def exit_reason(self) -> str:
        """The last thing the decoder said before it exited."""
        code = self._proc.poll() if self._proc is not None else None
        lines: list[str] = []
        if self._dir is not None:
            try:
                text = (self._dir / "decoder.log").read_bytes()[-4000:].decode("utf-8", "replace")
                lines = [line.strip() for line in text.splitlines() if line.strip()]
            except OSError:
                pass
        # Skip its closing pleasantries; the cause is the line before them.
        useful = [line for line in lines if not re.search(r"exit\.?$|Waiting for receive thread", line)]
        return (useful or lines or [f"exit code {code}"])[-1][-160:]

    def read(self) -> tuple[list[Aircraft], int, str]:
        """(aircraft, messages decoded so far, error). The error is set once the decoder has died."""
        if self._proc is None:
            return [], 0, "ADS-B decoder is not running"
        if self._proc.poll() is not None:
            return [], self._last[1], f"{self.program} stopped: {self.exit_reason()}"
        try:
            self._last = parse_aircraft_json((self._dir / "aircraft.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass                                   # not written yet: first second after starting
        except (OSError, ValueError) as exc:
            log.warning("unreadable aircraft.json: %s", exc)
        return self._last[0], self._last[1], ""

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            except OSError as exc:
                log.warning("could not stop %s: %s", self.program, exc)
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._last = ([], 0)
