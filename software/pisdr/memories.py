"""Named frequency memories saved under ``~/.config/pisdr/memories.json``."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from . import config

log = logging.getLogger(__name__)


@dataclass
class Memory:
    name: str
    vfo_hz: int
    mode: str
    step_hz: int

    def label(self) -> str:
        return f"{self.name}\n{_fmt_mhz(self.vfo_hz)} {self.mode}"


def _fmt_mhz(vfo_hz: int) -> str:
    mhz = vfo_hz / 1e6
    if mhz >= 100:
        return f"{mhz:.3f}"
    if mhz >= 10:
        return f"{mhz:.4f}"
    return f"{mhz:.5f}"


def default_name(vfo_hz: int, mode: str) -> str:
    return f"{_fmt_mhz(vfo_hz)} {mode}"


def load_memories(path=None) -> list[Memory]:
    path = path or config.MEMORIES_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        memories = []
        for item in data.get("memories", []):
            mode = item.get("mode", "NFM")
            if mode not in config.MODES:
                mode = "NFM"
            memories.append(Memory(
                name=str(item.get("name") or default_name(int(item["vfo_hz"]), mode)),
                vfo_hz=int(item["vfo_hz"]),
                mode=mode,
                step_hz=int(item.get("step_hz", 1000)),
            ))
        return memories[: config.MAX_MEMORIES]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.debug("No memories loaded: %s", exc)
        return []


def save_memories(memories: list[Memory], path=None) -> None:
    path = path or config.MEMORIES_FILE
    try:
        config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"memories": [asdict(m) for m in memories[: config.MAX_MEMORIES]]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        log.warning("Could not save memories: %s", exc)
