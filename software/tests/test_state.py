"""RadioState: tuning rules, settings.json round trip, and surviving a damaged settings file."""
from __future__ import annotations

import json
import math

import pytest

from pisdr import config, state as state_mod, storage
from pisdr.memories import Memory
from pisdr.state import RadioState, format_freq, format_step


def changes(state: RadioState) -> list[str]:
    seen: list[str] = []
    state.changed.connect(seen.append)
    return seen


def assert_sane(s: RadioState) -> None:
    """Everything the rest of the program relies on without checking."""
    assert config.FREQ_MIN <= s.vfo_hz <= config.FREQ_MAX and isinstance(s.vfo_hz, int)
    assert s.mode in config.MODES
    assert 0 <= s.band_index < len(s.bands)
    assert s.step_hz in config.STEPS_HZ
    assert 0 <= s.volume <= 100
    assert config.SQUELCH_MIN_DB <= s.squelch_db <= config.SQUELCH_MAX_DB
    assert config.GAIN_MIN_DB <= s.gain_db <= config.GAIN_MAX_DB
    assert config.FFT_FPS_MIN <= s.fft_fps <= config.FFT_FPS_MAX
    assert 1 <= s.waterfall_speed <= config.WATERFALL_SPEED_MAX
    assert s.spectrum_zoom in config.SPECTRUM_ZOOMS
    assert s.theme in config.THEME_NAMES
    assert s.decoder in config.DECODERS
    assert s.adsb_range_nm in [nm for nm, _ring in config.ADSB_RANGES_NM] and s.adsb_labels in config.ADSB_LABELS
    assert not (s.adsb_mode and s.network_mode), "the dongle cannot be lent twice"
    assert s.filter_hz in config.FILTER_WIDTHS[s.mode]
    assert s.mode_spec.bandwidth_hz > 0
    assert 0 <= s.spectrum_avg < len(config.SPECTRUM_AVG_NAMES)
    assert config.BRIGHTNESS_MIN_PCT <= s.brightness <= 100
    assert s.dim_after_s in config.DIM_AFTER_CHOICES_S
    assert 0 <= s.timer_start_min < 24 * 60
    assert isinstance(s.lockouts, list)
    assert not s.timer_armed or s.timer_target is not None
    for value in (s.squelch_db, s.gain_db, s.smeter_cal_db, s.scan_floor_db):
        assert math.isfinite(value)
    for key in ("vfo_hz", "volume", "spectrum_zoom", "fft_fps", "scan_dwell_ms", "ppm", "step_index"):
        assert type(getattr(s, key)) is int, key          # True or 2.0 would break arithmetic later
    for key in ("agc", "bias_tee", "dsp_hpf", "peak_hold", "knob_accel", "timer_armed", "rds_enabled"):
        assert type(getattr(s, key)) is bool, key
    assert (s.wx_lat is None) == (s.wx_lon is None)
    assert s.wx_zip == "" or (s.wx_lat is not None and len(s.wx_zip) == 5 and s.wx_zip.isdigit())
    assert all(config.FREQ_MIN <= hz <= config.FREQ_MAX for hz in s.lockouts)
    for target in (s.priority, s.timer_target):
        assert target is None or config.FREQ_MIN <= target["vfo_hz"] <= config.FREQ_MAX


# --------------------------------------------------------------------------- tuning
def test_tuning_snaps_to_the_step_grid(state):
    state.set_step_hz(5000)
    state.set_vfo(146_521_300)
    state.tune_steps(1)
    assert state.vfo_hz == 146_525_000
    state.tune_steps(-2)
    assert state.vfo_hz == 146_515_000


def test_vfo_is_clamped_to_the_dongle_range(state):
    state.set_vfo(1)
    assert state.vfo_hz == config.FREQ_MIN
    state.set_vfo(9e12)
    assert state.vfo_hz == config.FREQ_MAX


def test_band_follows_the_vfo(state):
    target = next(b for b in state.bands if b.name != state.band.name)
    state.set_vfo(target.default_hz)
    assert state.band.contains(target.default_hz)


def test_select_band_applies_mode_step_and_frequency(state):
    for index, band in enumerate(state.bands):
        state.select_band(index)
        assert (state.vfo_hz, state.mode) == (band.default_hz, band.mode)
        assert state.step_hz == band.step_hz
        assert state.band.name == band.name, "picked one band, landed in an overlapping one"


def test_band_key_reaches_every_band(state):
    """"HF utility" spans 40 m, 20 m and the WWV slots; cycling used to stick on it."""
    state.select_band(0)
    seen = []
    for _ in state.bands:
        state.cycle_band()
        seen.append(state.band.name)
    assert sorted(seen) == sorted(band.name for band in state.bands)


def test_overlapping_bands_keep_the_current_one_then_take_the_narrowest(state):
    names = [band.name for band in state.bands]
    state.select_band(names.index("HF utility"))
    state.set_vfo(7_100_000)                       # still inside the band that was picked
    assert state.band.name == "HF utility"
    state.select_band(names.index("2 m"))
    state.set_vfo(7_100_000)                       # arriving from outside: the specific band
    assert state.band.name == "40 m"
    state.set_vfo(7_400_000)                       # off the top of 40 m
    assert state.band.name == "HF utility"
    state.set_vfo(200_000_000)                     # in no band at all: label stays put
    assert state.band.name == "HF utility"


def test_unchanged_value_emits_nothing(state):
    seen = changes(state)
    state.set_volume(state.volume)
    state.set_mode(state.mode)
    state.set_mode("FAX")          # unknown mode: ignored
    assert seen == []


def test_setting_gain_turns_tuner_agc_off(state):
    state.set_agc(True)
    state.set_gain(20)
    assert state.agc is False and state.gain_db == 20.0


def test_ssb_passband_sits_on_the_correct_side(state):
    state.set_mode("USB")
    assert state.mode_spec.offset_hz > 0
    low_edge = state.mode_spec.offset_hz - state.mode_spec.bandwidth_hz / 2
    assert low_edge == pytest.approx(config.SSB_LOW_CUT_HZ)
    state.set_mode("LSB")
    assert state.mode_spec.offset_hz < 0


def test_filter_width_is_remembered_per_mode(state):
    state.set_mode("AM")
    state.step_filter(1)
    am = state.filter_hz
    state.set_mode("CW")
    state.fit_filter(250)
    assert state.filter_hz == 300              # narrowest that still passes 250 Hz
    state.fit_filter(99_999)
    assert state.filter_hz == max(config.FILTER_WIDTHS["CW"])
    state.set_mode("AM")
    assert state.filter_hz == am


def test_zoom_steps_stop_at_the_ends(state):
    for _ in range(20):
        state.step_spectrum_zoom(+1)
    assert state.spectrum_zoom == config.SPECTRUM_ZOOMS[-1]
    for _ in range(20):
        state.step_spectrum_zoom(-1)
    assert state.spectrum_zoom == 1


# --------------------------------------------------------------------------- knob
def test_knob_controls_the_selected_function(state):
    state.set_knob_function("VOLUME")
    before = state.volume
    state.knob_turned(3)
    assert state.volume == before + 3 * config.VOLUME_STEP
    state.set_knob_function("SQUELCH")
    state.knob_turned(5)
    assert state.squelch_db == config.SQUELCH_MIN_DB + 5 * config.SQUELCH_STEP_DB


def test_knob_returns_to_tune_when_left_alone(state, clock, monkeypatch):
    monkeypatch.setattr(state_mod, "time", clock)
    state.set_knob_function("GAIN")
    clock.advance(config.FUNC_RETURN_S - 0.5)
    state._check_func_return()
    assert state.knob_function == "GAIN"
    clock.advance(1.0)
    state._check_func_return()
    assert state.knob_function == "TUNE"


def test_fast_spinning_tunes_in_bigger_steps(state, clock, monkeypatch):
    monkeypatch.setattr(state_mod, "time", clock)
    state.set_step_hz(1000)
    start = state.vfo_hz
    state.knob_turned(1)
    assert state.vfo_hz - start == 1000                  # one slow click = one step
    clock.advance(1.0)
    start = state.vfo_hz
    for _ in range(12):                                  # 12 clicks in 120 ms
        clock.advance(0.01)
        state.knob_turned(1)
    assert state.vfo_hz - start > 12 * 1000
    clock.advance(1.0)
    start = state.vfo_hz
    state.knob_turned(-1)                                # reversing drops back to single steps
    assert state.vfo_hz - start == -1000


def test_acceleration_can_be_switched_off(state, clock, monkeypatch):
    monkeypatch.setattr(state_mod, "time", clock)
    state.set_knob_accel(False)
    state.set_step_hz(1000)
    start = state.vfo_hz
    for _ in range(12):
        clock.advance(0.01)
        state.knob_turned(1)
    assert state.vfo_hz - start == 12_000


# --------------------------------------------------------------------------- persistence
def test_settings_round_trip(qapp):
    a = RadioState()
    a.tune_to(7_074_000, "USB", 100)
    a.set_volume(33)
    a.set_squelch(-71)
    a.set_theme("Inferno")
    a.set_dsp_nr_level(4)
    a.step_filter(1)
    a.lock_out(7_100_000)
    a.cycle_spectrum_zoom(3)
    a.set_priority(a.priority_from_current())
    a.arm_timer(True)
    a.save()

    b = RadioState()
    b.load()
    for key in RadioState.PERSISTED:
        assert getattr(b, key) == getattr(a, key), key
    assert_sane(b)


def test_changes_are_saved_after_the_debounce(state):
    state.set_volume(41)
    assert state._save_timer.isActive()
    assert not config.SETTINGS_FILE.exists()          # not on every knob click
    state._save_timer.timeout.emit()
    assert json.loads(config.SETTINGS_FILE.read_text())["volume"] == 41


def test_live_readings_are_not_persisted(state):
    state.set_signal_db(-50.0)
    state.set_recording(True)
    state.set_knob_function("GAIN")
    assert not state._save_timer.isActive()
    state.save()
    saved = json.loads(config.SETTINGS_FILE.read_text())
    assert "recording" not in saved and "signal_db" not in saved


GARBAGE = [None, "junk", "", -1, 0, 1e99, -1e99, float("nan"), float("inf"), 3.7, True,
           [], [1, "a", None], {}, {"a": 1}, "AM", 99999999999]


@pytest.mark.parametrize("key", RadioState.PERSISTED)
def test_any_single_bad_value_is_survived(qapp, key):
    for value in GARBAGE:
        good = RadioState()
        good.set_volume(77)
        good.save()
        data = json.loads(config.SETTINGS_FILE.read_text())
        data[key] = value
        config.SETTINGS_FILE.write_text(json.dumps(data))

        s = RadioState()
        s.load()                      # must not raise
        assert_sane(s)
        if key != "volume":
            assert s.volume == 77, f"{key}={value!r} wiped unrelated settings"
        s.save()                      # and must be writable again
        json.loads(config.SETTINGS_FILE.read_text())


def test_everything_bad_at_once_is_survived(qapp):
    for value in GARBAGE:
        config.SETTINGS_FILE.write_text(json.dumps({k: value for k in RadioState.PERSISTED}))
        s = RadioState()
        s.load()
        assert_sane(s)


@pytest.mark.parametrize("text", ["", "{", "[1, 2", '{"volume": 5', "\x00\x00\x00", "null", "[]", "7"])
def test_unreadable_settings_file_is_set_aside(qapp, text):
    config.SETTINGS_FILE.write_text(text)
    s = RadioState()
    s.load()
    assert_sane(s)
    assert not config.SETTINGS_FILE.exists()
    assert config.SETTINGS_FILE.with_name("settings.json.corrupt").read_text() == text


def test_bias_tee_only_comes_on_when_clearly_saved_on(qapp):
    """4.5 V on the antenna socket must never come from a sloppy value."""
    for value, expected in ((True, True), (1, False), ("true", False), ("yes", False), ([1], False)):
        config.SETTINGS_FILE.write_text(json.dumps({"bias_tee": value}))
        s = RadioState()
        s.load()
        assert s.bias_tee is expected, value


def test_settings_from_an_older_version_keep_their_values(qapp):
    config.SETTINGS_FILE.write_text(json.dumps({"vfo_hz": 162_550_000, "mode": "NFM", "volume": 12,
                                                "removed_setting": 1}))
    s = RadioState()
    s.load()
    assert (s.vfo_hz, s.mode, s.volume) == (162_550_000, "NFM", 12)
    assert s.theme == config.DEFAULT_THEME


def test_save_failure_is_logged_not_raised(state, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(storage, "write_json_atomic", full)
    state.save()
    state.add_scan_hit({"t": 1, "vfo_hz": 1, "mode": "AM", "name": "x", "peak_db": -50, "src": "band"})


# --------------------------------------------------------------------------- recording guard
def test_recording_is_refused_when_storage_is_low(state, monkeypatch):
    messages: list[str] = []
    state.message.connect(messages.append)
    monkeypatch.setattr(storage, "free_bytes", lambda d: 50_000_000)
    state.set_recording(True)
    assert state.recording is False
    assert "free space" in messages[-1]


def test_running_recording_stops_before_the_card_fills(state, monkeypatch):
    messages: list[str] = []
    state.message.connect(messages.append)
    free = {"bytes": 10_000_000_000}
    monkeypatch.setattr(storage, "free_bytes", lambda d: free["bytes"])
    state.set_recording(True)
    assert state.recording and state._rec_guard.isActive()
    state._check_recording_space()
    assert state.recording
    free["bytes"] = config.REC_STOP_FREE_MB * 1_000_000 - 1
    state._check_recording_space()
    assert state.recording is False and not state._rec_guard.isActive()
    assert "almost full" in messages[-1]


def test_unknown_free_space_does_not_block_recording(state, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda d: None)
    state.set_recording(True)
    assert state.recording


def test_recording_is_refused_during_playback_and_network_mode(state):
    state.set_playing("/tmp/x.wav")
    state.set_recording(True)
    assert not state.recording
    state.set_playing("")
    state.set_network_mode(True)
    state.set_recording(True)
    assert not state.recording


# --------------------------------------------------------------------------- memories
def test_memories_save_recall_delete(state):
    state.tune_to(14_074_000, "USB", 100)
    mem = state.save_current_memory("FT8 20m")
    assert mem in state.memories
    state.tune_to(100_100_000, "WFM", 100_000)
    state.recall_memory(state.memories.index(mem))
    assert (state.vfo_hz, state.mode, state.step_hz) == (14_074_000, "USB", 100)
    assert RadioState().memories == state.memories          # written to disk
    state.delete_memory(state.memories.index(mem))
    assert mem not in state.memories
    state.delete_memory(0)                                   # empty list: no error
    state.recall_memory(0)


def test_memory_list_is_capped(state):
    state.memories = [Memory(f"m{i}", 1_000_000 + i, "AM", 1000) for i in range(config.MAX_MEMORIES)]
    assert state.save_current_memory("one too many") is None
    assert len(state.memories) == config.MAX_MEMORIES


def test_presets_import_once(state):
    added = state.import_preset_memories()
    assert added > 20 and len(state.memories) == added
    assert state.import_preset_memories() == 0
    assert len(state.memories) == added
    assert len(state.memory_groups) > 1


def test_memory_group_filter_and_on_now(state):
    state.memories = [
        Memory("always", 5_000_000, "AM", 1000, group="A"),
        Memory("never", 6_000_000, "AM", 1000, group="A", utc="00:00-00:01", days="Mon"),
        Memory("other", 7_000_000, "AM", 1000, group="B"),
    ]
    assert state.memory_groups == ["ALL", "A", "B"]
    state.set_memory_group("A")
    assert state.memory_indices() == [0, 1]
    state.set_memory_group("nope")
    assert state.memory_group == "ALL"
    # A Wednesday at noon UTC: the Monday 00:00 window is closed.
    assert [state.memories[i].name for i in range(3) if state.memories[i].on_now(1_700_049_600)] == ["always", "other"]


def test_lockouts_toggle_and_cover_the_channel_width(state):
    state.set_mode("NFM")
    assert state.toggle_lockout(146_520_000) is True
    assert state.is_locked_out(146_521_000)                 # inside the same channel
    assert not state.is_locked_out(146_540_000)
    assert state.toggle_lockout(146_520_000) is False
    assert state.lockouts == []


def test_priority_cycles_through_memories_and_off(state):
    state.memories = [Memory("a", 5_000_000, "AM", 1000), Memory("b", 6_000_000, "AM", 1000)]
    names = []
    for _ in range(4):
        state.cycle_priority(1)
        names.append(state.priority["name"] if state.priority else None)
    assert names == ["a", "b", None, "a"]


def test_scan_log_is_kept_newest_first_and_capped(state):
    for i in range(config.SCAN_LOG_MAX + 5):
        state.add_scan_hit({"t": i, "vfo_hz": 7_000_000 + i, "mode": "AM", "name": str(i), "peak_db": -50, "src": "band"})
    assert len(state.scan_log) == config.SCAN_LOG_MAX
    assert state.scan_log[0]["t"] == config.SCAN_LOG_MAX + 4
    assert RadioState().scan_log == state.scan_log


def test_damaged_scan_log_entries_are_dropped(qapp):
    good = {"t": 1_700_000_000, "vfo_hz": 155_340_000, "mode": "NFM", "name": "EMS",
            "peak_db": -48.2, "src": "band", "dur_s": 12.5}
    bad = [{"oops": True}, 7, None, {**good, "vfo_hz": "x"}, {**good, "vfo_hz": 0}, {**good, "mode": "FAX"},
           {**good, "t": 1e300}, {**good, "dur_s": None}]
    config.SCAN_LOG_FILE.write_text(json.dumps({"hits": bad[:4] + [good] + bad[4:]}))
    assert RadioState().scan_log == [good]
    for text in ("[1, 2]", "{", '{"hits": 5}', ""):
        config.SCAN_LOG_FILE.write_text(text)
        assert RadioState().scan_log == []


def test_cw_text_buffer_is_bounded_and_collapses_spaces(state):
    state.append_cw_text("A")
    state.append_cw_text(" ")
    state.append_cw_text(" ")
    state.append_cw_text("B")
    assert state.cw_text == "A B"
    state.append_cw_text("X" * 2 * config.CW_TEXT_MAX)
    assert len(state.cw_text) == config.CW_TEXT_MAX


def test_rds_text_is_dropped_outside_wfm(state):
    state.set_mode("NFM")
    state.set_rds("ps", "LATE")
    assert state.rds_ps == ""
    state.set_mode("WFM")
    state.set_rds("ps", "KEXP")
    assert state.rds_ps == "KEXP"
    state.set_rds_enabled(False)
    assert state.rds_ps == ""


def test_formatting():
    assert format_freq(14_200_000) == "14.200.000"
    assert format_freq(1_766_000_000) == "1766.000.000"
    assert format_freq(500_000) == "0.500.000"
    assert [format_step(hz) for hz in (10, 1000, 12_500, 1_000_000)] == ["10 Hz", "1 kHz", "12.5 kHz", "1 MHz"]
