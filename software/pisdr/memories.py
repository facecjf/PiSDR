"""Named frequency memories saved under ``~/.config/pisdr/memories.json``.

A memory may carry a ``group`` (shown as a filter on the memories page and used by memory
scan) and a UTC schedule: ``utc`` is ``"HH:MM-HH:MM"`` and ``days`` an optional list such as
``"Mon,Wed"``. Memories with a schedule are "on now" only inside that window.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from . import config, storage

log = logging.getLogger(__name__)

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class Memory:
    name: str
    vfo_hz: int
    mode: str
    step_hz: int
    group: str = ""
    utc: str = ""      # "HH:MM-HH:MM" (UTC); empty = no schedule
    days: str = ""     # "Mon,Wed" (UTC day the window starts); empty = every day

    def label(self) -> str:
        return f"{self.name}\n{_fmt_mhz(self.vfo_hz)} {self.mode}"

    @property
    def scheduled(self) -> bool:
        return parse_window(self.utc) is not None

    def on_now(self, now: float | None = None) -> bool:
        """True when there is no schedule, or the current UTC time is inside it."""
        window = parse_window(self.utc)
        if window is None:
            return True
        return in_window(window, parse_days(self.days), time.gmtime(now))

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("group", "utc", "days"):
            if not data[key]:
                del data[key]
        return data


def parse_window(text: str) -> tuple[int, int] | None:
    """``"23:00-03:30"`` -> (1380, 210) minutes after 00:00 UTC, or None."""
    try:
        start, end = text.split("-")
        return _minutes(start), _minutes(end)
    except (AttributeError, ValueError):
        return None


def _minutes(hhmm: str) -> int:
    hh, mm = hhmm.strip().split(":")
    value = int(hh) * 60 + int(mm)
    if not 0 <= value <= 24 * 60:
        raise ValueError(hhmm)
    return value % (24 * 60)


def parse_days(text: str) -> set[int]:
    """``"Mon,Wed"`` -> {0, 2}; empty or unparseable -> all days."""
    days = {DAY_NAMES.index(d.strip()[:3].title()) for d in (text or "").split(",")
            if d.strip()[:3].title() in DAY_NAMES}
    return days or set(range(7))


def in_window(window: tuple[int, int], days: set[int], tm: time.struct_time) -> bool:
    start, end = window
    now_min = tm.tm_hour * 60 + tm.tm_min
    today = tm.tm_wday
    if start == end:          # "00:00-24:00": the whole day
        return today in days
    if start < end:
        return today in days and start <= now_min < end
    # Crosses midnight: before midnight it belongs to today, after midnight to yesterday.
    if now_min >= start:
        return today in days
    return now_min < end and (today - 1) % 7 in days


def _fmt_mhz(vfo_hz: int) -> str:
    mhz = vfo_hz / 1e6
    if mhz >= 100:
        return f"{mhz:.3f}"
    if mhz >= 10:
        return f"{mhz:.4f}"
    return f"{mhz:.5f}"


def default_name(vfo_hz: int, mode: str) -> str:
    return f"{_fmt_mhz(vfo_hz)} {mode}"


def memory_from_json(item) -> Memory | None:
    """One entry of memories.json as a :class:`Memory`, or None if it can't be used."""
    try:
        vfo_hz = int(float(item["vfo_hz"]))
        if not config.FREQ_MIN <= vfo_hz <= config.FREQ_MAX:
            return None
        mode = item.get("mode", "NFM")
        if mode not in config.MODES:
            mode = "NFM"
        try:
            step_hz = int(item.get("step_hz", 1000))
        except (TypeError, ValueError):
            step_hz = 1000
        utc = str(item.get("utc") or "")
        return Memory(
            name=str(item.get("name") or default_name(vfo_hz, mode)),
            vfo_hz=vfo_hz,
            mode=mode,
            step_hz=step_hz if step_hz > 0 else 1000,
            group=str(item.get("group") or "").strip().upper()[:12],
            utc=utc if parse_window(utc) else "",
            days=str(item.get("days") or ""),
        )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def load_memories(path=None) -> list[Memory]:
    """Read memories (or a preset list from *path*). Entries that can't be used are skipped.

    Nothing the user typed is ever thrown away: if their own file is unreadable it is renamed
    to ``memories.json.corrupt``, and if only some entries are bad a copy is kept under that
    name before the next save drops them.
    """
    own_file = path is None
    path = path or config.MEMORIES_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    except ValueError as exc:
        log.warning("%s is not valid JSON: %s", path, exc)
        if own_file:
            storage.set_aside(path)
        return []
    items = data.get("memories", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    memories = [m for m in map(memory_from_json, items) if m is not None]
    skipped = len(items) - len(memories)
    if skipped:
        log.warning("%s: skipped %d entr%s that could not be read", path, skipped,
                    "y" if skipped == 1 else "ies")
        if own_file:
            storage.keep_copy(path)
    return memories[: config.MAX_MEMORIES]


def save_memories(memories: list[Memory], path=None) -> None:
    path = path or config.MEMORIES_FILE
    try:
        payload = {"memories": [m.to_json() for m in memories[: config.MAX_MEMORIES]]}
        storage.write_json_atomic(path, payload)
    except OSError as exc:
        log.warning("Could not save memories: %s", exc)
