"""Crash-safe saving, and the data files shipped with the project."""
from __future__ import annotations

import json
import os

import pytest

from pisdr import config, memories, storage, tags


def test_atomic_write_replaces_the_file(tmp_path):
    path = tmp_path / "sub" / "a.json"
    storage.write_json_atomic(path, {"a": 1})
    storage.write_json_atomic(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}
    assert [p.name for p in path.parent.iterdir()] == ["a.json"]     # no temp files left


def test_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "a.json"
    storage.write_json_atomic(path, {"good": True})

    def power_cut(src, dst):
        raise OSError("simulated failure before the rename")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", power_cut)
        with pytest.raises(OSError):
            storage.write_json_atomic(path, {"good": False})
    assert json.loads(path.read_text()) == {"good": True}
    assert [p.name for p in path.parent.iterdir()] == ["a.json"]


def test_free_bytes_uses_the_nearest_existing_folder(tmp_path):
    assert storage.free_bytes(tmp_path / "not" / "made" / "yet") > 0


# --------------------------------------------------------------------------- memories.json
def _write_memories(entries) -> None:
    config.MEMORIES_FILE.write_text(json.dumps({"memories": entries}))


GOOD = [{"name": "WWV", "vfo_hz": 10_000_000, "mode": "AM", "step_hz": 1000},
        {"name": "Club", "vfo_hz": 146_940_000, "mode": "NFM", "step_hz": 5000}]


def test_one_bad_memory_does_not_lose_the_rest():
    _write_memories(GOOD + [{"name": "typo", "vfo_hz": "146.52 MHz", "mode": "NFM"}])
    loaded = memories.load_memories()
    assert [m.name for m in loaded] == ["WWV", "Club"]
    # The original (with the entry that could not be read) is kept before any save drops it.
    kept = config.MEMORIES_FILE.with_name("memories.json.corrupt")
    assert "146.52 MHz" in kept.read_text()


@pytest.mark.parametrize("entry", [
    None, 5, "text", [], {}, {"vfo_hz": None}, {"vfo_hz": 12}, {"vfo_hz": 9e12},
    {"name": "x", "mode": "AM"},
])
def test_unusable_memory_entries_are_skipped(entry):
    _write_memories(GOOD[:1] + [entry] + GOOD[1:])
    assert [m.name for m in memories.load_memories()] == ["WWV", "Club"]


def test_memory_fields_are_repaired_not_dropped():
    _write_memories([{"vfo_hz": "7074000", "mode": "FT8", "step_hz": "fast", "group": " dx net ",
                      "utc": "25:99-xx"}])
    (mem,) = memories.load_memories()
    assert (mem.vfo_hz, mem.mode, mem.step_hz, mem.group, mem.utc) == (7_074_000, "NFM", 1000, "DX NET", "")
    assert mem.name == "7.07400 NFM"


def test_unreadable_memories_file_is_set_aside_not_overwritten():
    config.MEMORIES_FILE.write_text('{"memories": [{"name": "WWV", "vfo_hz": 10000000')   # cut off
    assert memories.load_memories() == []
    assert not config.MEMORIES_FILE.exists()
    assert "WWV" in config.MEMORIES_FILE.with_name("memories.json.corrupt").read_text()


def test_memories_round_trip():
    mems = [memories.Memory("A", 7_000_000, "LSB", 100, group="HAM", utc="23:00-01:00", days="Fri")]
    memories.save_memories(mems)
    assert memories.load_memories() == mems


# --------------------------------------------------------------------------- schedules
@pytest.mark.parametrize("text, expected", [
    ("23:00-03:30", (1380, 210)), ("00:00-24:00", (0, 0)), ("7:05 - 8:00", (425, 480)),
    ("", None), ("noon", None), ("25:00-26:00", None), (None, None),
])
def test_parse_window(text, expected):
    assert memories.parse_window(text) == expected


def _tm(day: int, hour: int, minute: int = 0):
    import time
    # 2024-01-01 was a Monday (tm_wday 0).
    return time.gmtime(1704067200 + day * 86400 + hour * 3600 + minute * 60)


def test_window_inside_one_day():
    window = (10 * 60, 12 * 60)
    assert memories.in_window(window, {0}, _tm(0, 11))
    assert not memories.in_window(window, {0}, _tm(0, 12))        # end is exclusive
    assert not memories.in_window(window, {0}, _tm(1, 11))        # wrong day


def test_window_across_midnight_belongs_to_the_day_it_starts():
    window = (23 * 60, 2 * 60)
    friday = {4}
    assert memories.in_window(window, friday, _tm(4, 23, 30))
    assert memories.in_window(window, friday, _tm(5, 1))          # Saturday 01:00 = Friday's window
    assert not memories.in_window(window, friday, _tm(4, 1))      # Friday 01:00 = Thursday's window
    assert not memories.in_window(window, friday, _tm(5, 2))


def test_equal_start_and_end_means_all_day():
    window = memories.parse_window("00:00-24:00")
    assert memories.in_window(window, {0}, _tm(0, 15))
    assert not memories.in_window(window, {0}, _tm(1, 15))


def test_parse_days():
    assert memories.parse_days("Mon, wed,FRIDAY") == {0, 2, 4}
    assert memories.parse_days("") == set(range(7))
    assert memories.parse_days("someday") == set(range(7))


# --------------------------------------------------------------------------- shipped data
def test_every_shipped_preset_loads():
    raw = json.loads(config.PRESET_MEMORIES_FILE.read_text())["memories"]
    loaded = memories.load_memories(config.PRESET_MEMORIES_FILE)
    assert len(loaded) == len(raw) > 20
    assert len(loaded) <= config.MAX_MEMORIES
    for item, mem in zip(raw, loaded):
        assert mem.mode == item["mode"], f"{item['name']}: unknown mode"
        if item.get("utc"):
            assert mem.utc == item["utc"], f"{item['name']}: schedule {item['utc']!r} does not parse"
    assert len({(m.vfo_hz, m.mode) for m in loaded}) == len(loaded), "duplicate preset"


def test_shipped_bands_are_consistent():
    raw = json.loads(config.BANDS_FILE.read_text())["bands"]
    bands = config.load_bands()
    assert len(bands) == len(raw) > 5
    for band in bands:
        assert band.mode in config.MODES, band.name
        assert config.FREQ_MIN <= band.start_hz < band.stop_hz <= config.FREQ_MAX, band.name
        assert band.contains(band.default_hz), band.name
        assert band.step_hz in config.STEPS_HZ, band.name


def test_shipped_tags_are_valid():
    raw = json.loads(config.TAGS_FILE.read_text())["tags"]
    loaded = tags.load_tag_file(config.TAGS_FILE, "builtin")
    assert len(loaded) == len(raw) > 50
    assert all(config.FREQ_MIN <= t.hz <= config.FREQ_MAX for t in loaded)
    assert all(len(str(item["label"])) <= 20 for item in raw), "label would be cut off"
    assert len({t.hz for t in loaded}) == len(loaded), "two tags on one frequency"


def test_bands_fall_back_when_the_file_is_missing(tmp_path):
    assert len(config.load_bands(tmp_path / "nope.json")) >= 2
