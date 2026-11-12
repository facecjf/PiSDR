"""Scanner and record timer, driven with a fake clock (no waiting, no radio)."""
from __future__ import annotations

import calendar

import pytest

from pisdr import config, rec_timer as rec_timer_mod, scan as scan_mod, storage
from pisdr.memories import Memory
from pisdr.rec_timer import RecTimer
from pisdr.scan import Scanner

QUIET, LOUD = -110.0, -40.0
TICK_S = config.SCAN_TICK_MS / 1000


class Rig:
    """A scanner on a fake clock. ``run(seconds)`` ticks it the way its QTimer would."""

    def __init__(self, state, clock) -> None:
        self.state = state
        self.clock = clock
        self.scanner = Scanner(state)
        self.status: list[str] = []
        self.scanner.status.connect(self.status.append)
        self.visited: list[int] = []
        state.changed.connect(lambda field: field == "vfo_hz" and self.visited.append(state.vfo_hz))
        state.set_signal_db(QUIET)

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / TICK_S)):
            self.clock.advance(TICK_S)
            self.scanner._tick()

    def start(self, source: str = "band") -> None:
        self.scanner.start(source)
        self.scanner._timer.stop()        # the test drives the ticks


@pytest.fixture
def rig(state, clock, monkeypatch):
    monkeypatch.setattr(scan_mod, "time", clock)
    band = next(i for i, b in enumerate(state.bands) if b.mode == "NFM")
    state.select_band(band)
    return Rig(state, clock)


# --------------------------------------------------------------------------- band scan
def test_quiet_channels_are_stepped_through_at_the_dwell_time(rig):
    s = rig.state
    start = s.vfo_hz
    rig.start()
    rig.run(s.scan_dwell_ms / 1000 * 5 + TICK_S)
    steps = [(b - a) for a, b in zip([start] + rig.visited, rig.visited)]
    assert 3 <= len(steps) <= 5
    assert set(steps) == {s.step_hz}


def test_scan_wraps_at_the_top_of_the_band(rig):
    s = rig.state
    s.set_vfo(s.band.stop_hz)
    rig.start()
    rig.run(s.scan_dwell_ms / 1000 + 2 * TICK_S)
    assert s.vfo_hz == s.band.start_hz
    assert s.scanning


def test_scan_holds_on_a_signal_and_resumes_when_it_ends(rig):
    s = rig.state
    rig.start()
    s.set_signal_db(LOUD)
    rig.run(config.SCAN_HOLD_MS / 1000 + 2 * TICK_S)
    assert s.scan_held
    held_on = s.vfo_hz
    rig.run(30)                               # still talking: stays put
    assert s.vfo_hz == held_on and s.scan_held

    s.set_signal_db(QUIET)
    rig.run(s.scan_resume_ms / 1000 - 0.5)
    assert s.scan_held, "resumed before the resume time"
    rig.run(1.0)
    assert not s.scan_held and s.scanning
    hit = s.scan_log[0]
    assert (hit["vfo_hz"], hit["src"]) == (held_on, "band")
    assert hit["peak_db"] == LOUD and 30 < hit["dur_s"] < 34


def test_short_noise_burst_does_not_stop_the_scan(rig):
    s = rig.state
    rig.start()
    s.set_signal_db(LOUD)
    rig.run(TICK_S)                           # one tick only, shorter than SCAN_HOLD_MS
    s.set_signal_db(QUIET)
    rig.run(1.0)
    assert not s.scan_held and s.scan_log == []


def test_resume_time_zero_waits_for_the_user(rig):
    s = rig.state
    while s.scan_resume_ms != 0:
        s.cycle_scan_resume()
    rig.start()
    s.set_signal_db(LOUD)
    rig.run(0.5)
    s.set_signal_db(QUIET)
    rig.run(120)
    assert s.scan_held
    on = s.vfo_hz
    rig.scanner.toggle_hold()                 # RESUME key moves on
    assert not s.scan_held and s.vfo_hz != on


def test_manual_hold_pauses_until_released(rig):
    s = rig.state
    rig.start()
    rig.scanner.toggle_hold()
    on = s.vfo_hz
    rig.run(10)
    assert s.vfo_hz == on and s.scan_held
    rig.scanner.toggle_hold()
    rig.run(s.scan_dwell_ms / 1000 + 2 * TICK_S)
    assert s.vfo_hz != on


def test_locked_out_channels_are_skipped(rig):
    s = rig.state
    start = s.vfo_hz
    s.lock_out(start + s.step_hz)
    s.lock_out(start + 2 * s.step_hz)
    rig.start()
    rig.run(s.scan_dwell_ms / 1000 + 2 * TICK_S)
    assert s.vfo_hz == start + 3 * s.step_hz


def test_long_skip_locks_the_channel_out(rig):
    s = rig.state
    rig.start()
    s.set_signal_db(LOUD)
    rig.run(0.5)
    noisy = s.vfo_hz
    rig.scanner.lockout_current()
    assert s.is_locked_out(noisy) and s.vfo_hz != noisy and s.scanning
    s.set_signal_db(QUIET)
    del rig.visited[:]
    rig.run(s.scan_dwell_ms / 1000 * 1200)    # several times round the band
    assert len(rig.visited) > 900 and noisy not in rig.visited


def test_scan_stops_when_everything_is_locked_out(rig):
    s = rig.state
    s.set_step_hz(1_000_000)
    hz = int(round(s.band.start_hz / s.step_hz) * s.step_hz)
    while hz <= s.band.stop_hz + s.step_hz:
        s.lock_out(hz)
        hz += s.step_hz
    s.lock_out(s.vfo_hz)
    rig.start()
    rig.run(2)
    assert not s.scanning
    assert "locked out" in rig.status[-1]


def test_squelch_above_the_floor_raises_the_trigger_level(rig):
    s = rig.state
    s.set_squelch(-50)
    rig.start()
    s.set_signal_db(-60)                      # above the scan floor, below the squelch
    rig.run(2)
    assert not s.scan_held


def test_stop_logs_the_hit_in_progress(rig):
    s = rig.state
    rig.start()
    s.set_signal_db(LOUD)
    rig.run(3)
    rig.scanner.stop()
    assert not s.scanning and len(s.scan_log) == 1
    assert rig.status[-1] == "Scan stopped"


# --------------------------------------------------------------------------- memory scan
def _memories(state) -> None:
    state.memories = [
        Memory("a", 146_520_000, "NFM", 5000, group="HAM"),
        Memory("b", 162_550_000, "NFM", 25000, group="WX"),
        Memory("c", 146_940_000, "NFM", 5000, group="HAM"),
        Memory("d", 7_200_000, "LSB", 100, group="HAM", utc="00:00-00:01", days="Mon"),
    ]


def test_memory_scan_cycles_the_selected_group(rig):
    s = rig.state
    _memories(s)
    s.set_memory_group("HAM")
    s.set_memory_scan_on_now(True)            # "d" is off the air: skipped
    rig.start("memory")
    rig.run(s.scan_dwell_ms / 1000 * 6)
    assert set(rig.visited) == {146_520_000, 146_940_000}
    assert s.mode == "NFM"


def test_memory_scan_includes_scheduled_memories_unless_on_now_only(rig):
    s = rig.state
    _memories(s)
    s.set_memory_group("HAM")
    rig.start("memory")
    rig.run(s.scan_dwell_ms / 1000 * 8)
    assert 7_200_000 in rig.visited


def test_memory_scan_refuses_to_start_with_nothing_to_scan(rig):
    rig.state.memories = []
    rig.start("memory")
    assert not rig.state.scanning
    assert "No memories" in rig.status[-1]


def test_deleting_the_last_memory_mid_scan_stops_cleanly(rig):
    s = rig.state
    _memories(s)
    rig.start("memory")
    rig.run(1)
    s.memories = []
    rig.run(2)
    assert not s.scanning


# --------------------------------------------------------------------------- priority
def _priority(state) -> dict:
    target = {"name": "CALL", "vfo_hz": 146_520_000, "mode": "NFM", "step_hz": 5000}
    state.set_priority(target)
    return target


def test_priority_channel_is_checked_and_scan_returns(rig):
    s = rig.state
    target = _priority(s)
    s.set_vfo(s.band.start_hz + 10 * s.step_hz)
    rig.start()
    rig.run(s.priority_interval_s + 3 * TICK_S)
    assert s.vfo_hz == target["vfo_hz"], "did not look at the priority channel"
    before = [hz for hz in rig.visited if hz != target["vfo_hz"]][-1]
    looked_ms = 0
    while s.vfo_hz == target["vfo_hz"] and looked_ms < 5000:
        rig.run(TICK_S)
        looked_ms += config.SCAN_TICK_MS
    assert s.vfo_hz == before, "did not return to where the scan was"
    assert looked_ms <= config.PRIORITY_LOOK_MS + config.PRIORITY_SETTLE_MS + 200, "look took too long"
    assert s.scanning and not s.scan_held and s.scan_log == []


def test_active_priority_channel_takes_over(rig):
    s = rig.state
    target = _priority(s)
    s.set_vfo(s.band.start_hz + 10 * s.step_hz)
    rig.start()
    rig.run(s.priority_interval_s + 3 * TICK_S)
    s.set_signal_db(LOUD)
    rig.run(1.0)
    assert s.scan_held and s.vfo_hz == target["vfo_hz"]
    assert rig.status[-1] == "Priority: CALL"
    s.set_signal_db(QUIET)
    rig.run(s.scan_resume_ms / 1000 + 1)
    assert not s.scan_held and s.vfo_hz != target["vfo_hz"]
    assert s.scan_log[0]["src"] == "priority" and s.scan_log[0]["name"] == "CALL"


def test_stopping_during_a_priority_look_returns_to_the_scan_channel(rig):
    s = rig.state
    _priority(s)
    s.set_vfo(s.band.start_hz + 10 * s.step_hz)
    rig.start()
    rig.run(s.priority_interval_s + 3 * TICK_S)
    was_on = [hz for hz in rig.visited if hz != 146_520_000][-1]
    rig.scanner.stop()
    assert s.vfo_hz == was_on


def test_priority_channel_cannot_be_locked_out(rig):
    s = rig.state
    target = _priority(s)
    s.set_vfo(s.band.start_hz + 10 * s.step_hz)
    rig.start()
    rig.run(s.priority_interval_s + 3 * TICK_S)
    rig.scanner.lockout_current()
    assert not s.is_locked_out(target["vfo_hz"])


# --------------------------------------------------------------------------- record timer
def utc(hour: int, minute: int = 0, day: int = 15) -> float:
    return float(calendar.timegm((2025, 1, day, hour, minute, 0)))


@pytest.fixture
def timer(state, clock, monkeypatch):
    monkeypatch.setattr(rec_timer_mod, "time", clock)
    monkeypatch.setattr(storage, "free_bytes", lambda d: 10_000_000_000)
    t = RecTimer(state)
    t._timer.stop()
    t.status_log = []
    t.status.connect(t.status_log.append)
    return t


def arm(state, start_min: int, length_min: int = 30, daily: bool = False) -> None:
    state.tune_to(6_840_000, "USB", 100)                  # the frequency to record
    state.adjust_timer_start(start_min - state.timer_start_min)
    while state.timer_length_min != length_min:
        state.cycle_timer_length()
    state.set_timer_daily(daily)
    state.arm_timer(True)
    state.tune_to(100_100_000, "WFM", 100_000)            # then the user goes elsewhere


def run_until(timer, clock, start: float, minutes: float) -> None:
    """Call check() every 5 s of fake time, like the real QTimer."""
    clock.wall = start
    for _ in range(int(minutes * 60 / 5)):
        clock.advance(5)
        timer.check(clock.wall)


def test_timer_records_the_armed_frequency_then_restores(state, timer, clock):
    arm(state, 20 * 60, length_min=30)
    timer.check(utc(19, 59))
    assert not state.recording

    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    assert state.recording and state.timer_running
    assert (state.vfo_hz, state.mode) == (6_840_000, "USB")

    run_until(timer, clock, utc(20, 0), 29)
    assert state.recording
    run_until(timer, clock, clock.wall, 2)
    assert not state.recording and not state.timer_running
    assert (state.vfo_hz, state.mode) == (100_100_000, "WFM")     # back where the user was
    assert not state.timer_armed                                  # one-shot
    assert timer.status_log[-1] == "Timer recording finished"


def test_timer_started_late_records_the_remainder(state, timer, clock):
    arm(state, 20 * 60, length_min=30)
    clock.wall = utc(20, 20)                   # e.g. the Pi was switched on late
    timer.check(clock.wall)
    assert state.recording
    run_until(timer, clock, clock.wall, 9)
    assert state.recording
    run_until(timer, clock, clock.wall, 2)
    assert not state.recording


def test_timer_does_not_start_after_its_window(state, timer):
    arm(state, 20 * 60, length_min=30)
    timer.check(utc(20, 30))
    timer.check(utc(23, 0))
    assert not state.recording and state.timer_armed


def test_window_across_midnight(state, timer, clock):
    arm(state, 23 * 60 + 50, length_min=30)
    clock.wall = utc(0, 5, day=16)
    timer.check(clock.wall)
    assert state.recording
    run_until(timer, clock, clock.wall, 16)
    assert not state.recording


def test_daily_timer_fires_once_a_day(state, timer, clock):
    arm(state, 20 * 60, length_min=5, daily=True)
    starts = []
    state.changed.connect(lambda f: f == "recording" and state.recording and starts.append(clock.wall))
    run_until(timer, clock, utc(19, 0, day=15), 60 * 40)          # two evenings
    assert [round((t - utc(20, 0)) / 86400, 3) for t in starts] == [0.0, 1.0]
    assert state.timer_armed


def test_stopping_the_recording_by_hand_ends_the_timer(state, timer, clock):
    arm(state, 20 * 60)
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    state.set_recording(False)
    assert not state.timer_running and not state.timer_armed
    assert state.vfo_hz == 6_840_000                              # stays where the user is listening
    timer.check(utc(20, 1))
    assert not state.recording                                    # and does not restart


def test_disarming_stops_a_timer_recording(state, timer, clock):
    arm(state, 20 * 60, daily=True)
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    state.arm_timer(False)
    assert not state.recording and not state.timer_running


def test_timer_gives_up_cleanly_when_recording_is_refused(state, timer, clock, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda d: 1_000_000)   # card full
    arm(state, 20 * 60)
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    assert not state.recording and not state.timer_running
    assert (state.vfo_hz, state.mode) == (100_100_000, "WFM"), "left the radio retuned for nothing"


def test_timer_waits_while_the_dongle_is_lent_to_the_network(state, timer, clock):
    arm(state, 20 * 60)
    state.set_network_mode(True)
    timer.check(utc(20, 0))
    assert not state.recording and state.vfo_hz == 100_100_000


def test_minutes_until_start(state, timer):
    arm(state, 20 * 60)
    assert timer.minutes_until_start(utc(19, 45)) == 15
    assert timer.minutes_until_start(utc(20, 15)) == 24 * 60 - 15
