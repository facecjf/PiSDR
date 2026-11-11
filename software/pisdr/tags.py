"""Frequency tags: names drawn on the spectrum at known frequencies.

Three sources, highest priority first:

* your memories (their names),
* ``~/.config/pisdr/tags.json``: your own tags plus FM station names learned from RDS,
* ``software/config/tags.json``: a built-in list of well-known frequencies.

An FM station name is learned once the RDS station name (PS) has stayed the same for a
few seconds, so stations that scroll text through the PS field don't create tags.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import config
from .qt import QObject, QTimer, Signal
from .state import RadioState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tag:
    hz: int
    label: str
    source: str     # "memory", "user", "rds" or "builtin"


def load_tag_file(path: Path, source: str) -> list[Tag]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    entries = data.get("tags", []) if isinstance(data, dict) else data
    tags = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            label = str(entry["label"]).strip()[:20]
            if label:
                tags.append(Tag(int(entry["hz"]), label, str(entry.get("src", source))))
        except (KeyError, TypeError, ValueError):
            continue
    return tags


class TagStore(QObject):
    changed = Signal()

    def __init__(self, state: RadioState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.builtin = load_tag_file(config.TAGS_FILE, "builtin")
        self.user = load_tag_file(config.USER_TAGS_FILE, "user")
        self._rds_candidate: tuple[int, str] | None = None
        self._rds_timer = QTimer(self)
        self._rds_timer.setSingleShot(True)
        self._rds_timer.setInterval(int(config.RDS_TAG_STABLE_S * 1000))
        self._rds_timer.timeout.connect(self._check_rds)
        state.changed.connect(self._on_state_changed)

    def tags_between(self, lo_hz: float, hi_hz: float) -> list[Tag]:
        """Tags inside [lo_hz, hi_hz], one per frequency, sorted by frequency."""
        chosen: dict[int, Tag] = {}
        sources = ([Tag(m.vfo_hz, m.name, "memory") for m in self.state.memories],
                   self.user, self.builtin)
        for tags in sources:
            for tag in tags:
                if lo_hz <= tag.hz <= hi_hz and tag.hz not in chosen:
                    chosen[tag.hz] = tag
        return sorted(chosen.values(), key=lambda t: t.hz)

    def label_near(self, hz: float, tolerance_hz: float) -> str:
        tags = self.tags_between(hz - tolerance_hz, hz + tolerance_hz)
        return min(tags, key=lambda t: abs(t.hz - hz)).label if tags else ""

    # ------------------------------------------------------------------ RDS learning
    def _on_state_changed(self, field: str) -> None:
        if field == "memories":
            self.changed.emit()
        elif field == "rds":
            self.observe_rds(self.state.vfo_hz, self.state.rds_ps)

    def observe_rds(self, hz: int, ps: str) -> None:
        ps = ps.strip()
        if not ps or self.state.mode != "WFM":
            self._rds_candidate = None
            self._rds_timer.stop()
        elif self._rds_candidate != (hz, ps):
            self._rds_candidate = (hz, ps)
            self._rds_timer.start()

    def _check_rds(self) -> None:
        s = self.state
        if s.mode == "WFM" and self._rds_candidate == (s.vfo_hz, s.rds_ps.strip()):
            self._learn(*self._rds_candidate)

    def _learn(self, hz: int, label: str) -> None:
        current = next((t for t in self.user if t.hz == hz), None)
        if current is not None and (current.label == label or current.source == "user"):
            return      # already known, or named by hand (never overwritten)
        self.user = [t for t in self.user if t.hz != hz] + [Tag(hz, label, "rds")]
        self._save()
        self.changed.emit()
        log.info("learned FM tag %s at %.1f MHz", label, hz / 1e6)

    def _save(self) -> None:
        try:
            config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {"tags": [{"hz": t.hz, "label": t.label, "src": t.source}
                             for t in sorted(self.user, key=lambda t: t.hz)]}
            with open(config.USER_TAGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
        except OSError as exc:
            log.warning("Could not save tags: %s", exc)
