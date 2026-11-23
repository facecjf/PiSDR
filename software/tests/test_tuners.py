"""Two dongles: what each receives, which one tunes a frequency, telling them apart, and how
both back ends move the receiver between them and share them with the ADS-B decoder.
"""
from __future__ import annotations

import stat

import pytest

import fakegr
from conftest import needs_fake_gr, pump
from pisdr import config
from pisdr.radio import adsb_decoder, tuners
from pisdr.radio.tuners import GENERIC, V3, V4, XTR, Dongle, Lineup, Source
from pisdr.state import RadioState
from pisdr.ui import sliders, vfo_bar
from test_adsb import FA_HELP, MUTABILITY_HELP, decoder_env, fake_decoder, script  # noqa: F401  (decoder_env: fixture)
from test_scan_and_timer import arm, timer, utc  # noqa: F401  (timer is a fixture)
from test_radio import TWO_DONGLES, fake_rtl_tcp, open_sources, radio, radio2, sim, sim_one  # noqa: F401  (fixtures)
from test_ui import rows

REAL_SCAN = tuners.scan           # conftest replaces it for every test
PAIR = Lineup(Dongle(0, V3), Dongle(1, XTR))                        # main tuner, ADS-B dongle
FM, HF, SIX_M, GAP, ADSB_HZ = 100_100_000, 7_200_000, 50_125_000, 1_200_000_000, config.ADSB_FREQ_HZ
HIGH = 2_000_000_000                                                # above the V3: only the XTR tunes it


# --------------------------------------------------------------------------- what each dongle receives
def test_what_each_dongle_covers():
    assert XTR.covers(FM) and XTR.covers(2_200_000_000) and XTR.covers(1_090_000_000)
    assert not XTR.covers(HF) and not XTR.covers(SIX_M) and not XTR.covers(GAP)
    assert XTR.distance(60_000_000) == 5_000_000 and XTR.distance(1_150_000_000) == 50_000_000
    assert V3.covers(HF) and V3.covers(SIX_M) and V3.covers(GAP) and V3.covers(config.FREQ_MIN)
    assert not V3.covers(2_000_000_000)
    assert V3.direct_at(HF) and not V3.direct_at(24_000_000) and not XTR.direct_at(HF) and not V4.direct_at(HF)
    assert V4.covers(HF) and GENERIC.covers(config.FREQ_MIN) and GENERIC.covers(config.FREQ_MAX)
    assert XTR.gain_max_db < V3.gain_max_db == config.GAIN_MAX_DB
    assert V3.bias_tee and not XTR.bias_tee


def test_every_band_and_the_whole_range_has_a_tuner(state):
    for band in state.bands:
        for hz in (band.start_hz, band.default_hz, band.stop_hz):
            assert PAIR.route(hz).covers(hz), f"nothing receives {band.name} at {hz}"
    assert PAIR.route(config.FREQ_MIN).covers(config.FREQ_MIN)
    assert PAIR.route(config.FREQ_MAX).covers(config.FREQ_MAX)


@pytest.mark.parametrize("hz, label", (
    (HF, "V3 HF"), (1_000_000, "V3 HF"), (23_999_000, "V3 HF"), (24_000_000, "V3"),
    (27_185_000, "V3"), (SIX_M, "V3"), (FM, "V3"), (446_000_000, "V3"), (ADSB_HZ, "V3"), (GAP, "V3"),
    (1_300_000_000, "V3"), (1_766_000_000, "V3"), (1_766_100_000, "XTR"), (HIGH, "XTR"), (2_300_000_000, "XTR"),
))
def test_the_main_tuner_takes_what_it_can_and_the_xtr_the_rest(hz, label):
    assert PAIR.route(hz).label == label


def test_a_tuner_can_be_insisted_on():
    assert PAIR.route(FM, "AUX").label == "XTR" and PAIR.route(1_300_000_000, "AUX").label == "XTR"
    assert PAIR.route(HIGH, "MAIN").label == "V3" and not PAIR.route(HIGH, "MAIN").covers(HIGH)
    assert PAIR.route(HF, "AUX").label == "XTR" and not PAIR.route(HF, "AUX").covers(HF)    # deaf, but as asked
    assert PAIR.named("MAIN") == PAIR.main and PAIR.named("AUX") == PAIR.aux and PAIR.named("AUTO") is None


def test_while_adsb_has_the_xtr_everything_goes_to_the_main_tuner():
    for prefer in config.TUNER_PREFS:
        assert PAIR.route(FM, prefer, aux_free=False).label == "V3"
        assert PAIR.route(HF, prefer, aux_free=False).label == "V3 HF"
        assert PAIR.route(HIGH, prefer, aux_free=False).label == "V3"


def test_one_dongle_does_everything():
    alone = Lineup(Dongle(0, V3))
    assert alone.adsb == alone.main and alone.dongles == [alone.main]
    for prefer in config.TUNER_PREFS:
        assert alone.route(HF, prefer).label == "V3 HF" and alone.route(FM, prefer, aux_free=False).label == "V3"
    assert Lineup().route(HF).label == "RTL" and Lineup().route(HF).device_args() == "rtl=0"
    assert PAIR.adsb == PAIR.aux and len(PAIR.dongles) == 2


def test_device_strings():
    assert Source(Dongle(1, XTR)).device_args() == "rtl=1"
    assert Source(Dongle(1, XTR)).device_args(bias_tee=True) == "rtl=1", "the XTR has no bias tee to switch"
    assert Source(Dongle(0, V3)).device_args(bias_tee=True) == "rtl=0,bias=1"
    assert Source(Dongle(0, V3), direct=True).device_args() == "rtl=0,direct_samp=2"
    assert Source(Dongle(0, V3), direct=True).device_args(True) == "rtl=0,direct_samp=2,bias=1"


# --------------------------------------------------------------------------- who is who
def test_dongles_are_known_by_their_tuner_chip():
    assert tuners.identify(1) is XTR and tuners.identify(5) is V3 and tuners.identify(6) is V4
    assert tuners.identify(1, "RTLSDRBlog", "Blog V4") is XTR, "the chip is what counts, not the label"
    assert tuners.identify(0, "Nooelec", "SMArt XTR v5") is XTR
    assert tuners.identify(0, "RTLSDRBlog", "Blog V4") is V4
    assert tuners.identify(0, "Realtek", "RTL2838UHIDIR") is GENERIC and tuners.identify(3) is GENERIC


def test_jobs_are_given_out_whichever_way_round_the_dongles_enumerate():
    xtr, v3 = Dongle(0, XTR, serial="00000001"), Dongle(1, V3, serial="00000001")
    assert tuners.assign([xtr, v3]) == Lineup(v3, xtr)
    xtr, v3 = Dongle(1, XTR), Dongle(0, V3)
    assert tuners.assign([v3, xtr]) == Lineup(v3, xtr)
    assert tuners.assign([v3]) == Lineup(v3) and tuners.assign([xtr]) == Lineup(xtr) and tuners.assign([]) == Lineup()
    first, second = Dongle(0), Dongle(1)                             # neither could be identified
    assert tuners.assign([first, second]) == Lineup(first, second)
    assert tuners.assign([first, xtr]) == Lineup(first, xtr), "whatever the other one is, the XTR gets ADS-B"
    unknown = Dongle(1)                                              # probably the XTR, busy when it was looked at
    assert tuners.assign([v3, unknown]) == Lineup(v3, unknown)
    assert tuners.assign([Dongle(0, V3), Dongle(1, V3)]) == Lineup(Dongle(0, V3), Dongle(1, V3))
    v4 = Dongle(2, V4)
    assert tuners.assign([v3, xtr, v4]) == Lineup(v4, xtr), "a V4 does HF properly: it is the receiver"
    assert tuners.assign([Dongle(0, XTR), Dongle(1, V4)]) == Lineup(Dongle(1, V4), Dongle(0, XTR))


def test_jobs_can_be_set_by_serial_number_or_index(monkeypatch):
    a, b = Dongle(0, V3, serial="ADSB"), Dongle(1, V3, serial="MAIN")
    assert tuners.assign([a, b]) == Lineup(a, b)
    monkeypatch.setattr(config, "MAIN_TUNER", "MAIN")
    assert tuners.assign([a, b]) == Lineup(b, a)
    monkeypatch.setattr(config, "MAIN_TUNER", "")
    monkeypatch.setattr(config, "ADSB_TUNER", "0")
    assert tuners.assign([a, b]) == Lineup(b, a)
    monkeypatch.setattr(config, "MAIN_TUNER", "nothing like it")     # a typo falls back to the chips
    monkeypatch.setattr(config, "ADSB_TUNER", "")
    xtr = Dongle(1, XTR, serial="X")
    assert tuners.assign([a, xtr]) == Lineup(a, xtr)
    monkeypatch.setattr(config, "MAIN_TUNER", "X")                   # the old arrangement, by request
    assert tuners.assign([a, xtr]) == Lineup(xtr, a)


class FakeLibrtlsdr:
    """librtlsdr as ctypes sees it, with two dongles of which the second can be made busy."""

    def __init__(self, devices: list[tuple[str, str, str, int]], busy: tuple[int, ...] = ()) -> None:
        self.devices, self.busy = devices, busy
        self.open: set[int] = set()
        self.opened: list[int] = []

    def rtlsdr_get_device_count(self) -> int:
        return len(self.devices)

    def rtlsdr_get_device_usb_strings(self, index, manufacturer, product, serial) -> int:
        for buffer, text in zip((manufacturer, product, serial), self.devices[index]):
            buffer.value = text.encode()
        return 0

    def rtlsdr_open(self, handle, index) -> int:
        if index in self.busy:
            return -6
        handle._obj.value = index + 1
        self.open.add(index)
        self.opened.append(index)
        return 0

    def rtlsdr_get_tuner_type(self, handle) -> int:
        return self.devices[handle.value - 1][3]

    def rtlsdr_close(self, handle) -> int:
        self.open.discard(handle.value - 1)
        return 0


def test_scan_reads_the_usb_strings_and_the_tuner_chip_and_lets_go(monkeypatch):
    lib = FakeLibrtlsdr([("RTLSDRBlog", "Blog V3", "00000001", 5), ("Nooelec", "SMArt XTR v5", "00000001", 1)])
    monkeypatch.setattr(tuners, "_library", lambda: lib)
    monkeypatch.setattr(tuners, "scan", REAL_SCAN)
    found = tuners.scan()
    assert [(d.index, d.spec.key, d.product, d.serial) for d in found] == \
        [(0, "V3", "Blog V3", "00000001"), (1, "XTR", "SMArt XTR v5", "00000001")]
    assert lib.opened == [0, 1] and not lib.open, "a dongle was left open: the receiver could not have it"
    lineup = tuners.detect()
    assert (lineup.main.spec, lineup.aux.spec) == (V3, XTR) and (lineup.main.index, lineup.aux.index) == (0, 1)
    assert lineup.aux.describe() == "#1 NESDR SMArt XTR (E4000) [Nooelec SMArt XTR v5, SN 00000001]"


def test_scan_copes_with_a_busy_dongle_and_with_no_library(monkeypatch):
    lib = FakeLibrtlsdr([("RTLSDRBlog", "Blog V3", "1", 5), ("Nooelec", "SMArt XTR v5", "2", 1)], busy=(1,))
    monkeypatch.setattr(tuners, "_library", lambda: lib)
    monkeypatch.setattr(tuners, "scan", REAL_SCAN)
    found = tuners.scan()
    assert [d.spec.key for d in found] == ["V3", "XTR"], "its name was there to go by"
    lib.devices[1] = ("Realtek", "RTL2838UHIDIR", "2", 1)
    assert [d.spec.key for d in tuners.scan()] == ["V3", "RTL"]
    assert tuners.detect() == Lineup(Dongle(0, V3, "RTLSDRBlog", "Blog V3", "1"),
                                     Dongle(1, GENERIC, "Realtek", "RTL2838UHIDIR", "2"))
    monkeypatch.setattr(tuners, "_library", lambda: None)
    assert tuners.scan() == [] and tuners.detect() == Lineup()


def test_bias_tee_of_an_idle_dongle_is_switched_with_rtl_biast(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert tuners.switch_bias_tee(1, True) is False, "no rtl_biast installed"
    log = tmp_path / "args"
    tool = tmp_path / "rtl_biast"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$2" != "7" ]\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert tuners.switch_bias_tee(1, True) and tuners.switch_bias_tee(0, False) and tuners.switch_bias_tee(None, True)
    assert tuners.switch_bias_tee(7, True) is False, "rtl_biast could not open the dongle"
    assert log.read_text().splitlines() == ["-d 1 -b 1", "-d 0 -b 0", "-b 1", "-d 7 -b 1"]


# --------------------------------------------------------------------------- state
def test_tuner_choice_cycles_and_persists(state):
    assert state.tuner_pref == "AUTO"
    state.cycle_tuner_pref()
    assert state.tuner_pref == "MAIN"
    state.cycle_tuner_pref(-2)
    assert state.tuner_pref == "AUX"
    state.save()
    again = RadioState()
    again.load()
    assert again.tuner_pref == "AUX" and again.tuner == "", "which dongle is in use is not a setting"
    state.tuner_pref = "BOTH"
    state.save()
    again.load()
    assert again.tuner_pref == "AUTO"


def test_gain_stops_where_the_tuner_in_use_stops(state):
    state.set_gain(99)
    assert state.gain_db == config.GAIN_MAX_DB
    seen = []
    state.changed.connect(seen.append)
    state.set_tuner("XTR", config.XTR_GAIN_MAX_DB)
    assert state.gain_db == config.XTR_GAIN_MAX_DB and seen == ["gain_db", "tuner"]
    state.set_gain(99)
    assert state.gain_db == config.XTR_GAIN_MAX_DB
    state.set_tuner("V3", config.GAIN_MAX_DB)
    state.set_gain(99)
    assert state.gain_db == config.GAIN_MAX_DB


# --------------------------------------------------------------------------- simulator
def test_simulator_moves_between_its_two_dongles(sim, state):   # noqa: F811
    assert state.adsb_own_dongle and [d.spec.key for d in sim.lineup.dongles] == ["V3", "XTR"]
    state.set_vfo(FM)
    assert state.tuner == "V3" and sim.source.label == "V3" and state.gain_max_db == config.GAIN_MAX_DB
    del sim.messages[:]
    state.set_vfo(HF)
    assert state.tuner == "V3 HF" and sim.messages == ["Tuner: V3 HF"]
    assert abs(state.vfo_hz - sim.center_hz) == pytest.approx(0.25 * config.SAMPLE_RATE)
    state.set_vfo(SIX_M)
    assert state.tuner == "V3"
    state.set_vfo(HIGH)
    assert state.tuner == "XTR" and state.gain_max_db == config.XTR_GAIN_MAX_DB
    state.set_vfo(FM)
    assert state.tuner == "V3"
    state.set_step_hz(5000)
    del sim.messages[:]
    for _ in range(40):                                              # ordinary tuning stays where it is
        state.tune_steps(1)
    assert sim.messages == []


def test_insisting_on_a_tuner_moves_the_receiver_and_says_when_it_is_deaf(sim, state):   # noqa: F811
    state.set_vfo(FM)
    state.cycle_tuner_pref(2)                                        # XTR only
    assert state.tuner == "XTR"
    state.cycle_tuner_pref(-1)                                       # V3 only
    assert state.tuner == "V3"
    del sim.messages[:]
    state.set_vfo(HIGH)
    assert state.tuner == "V3" and sim.messages == ["2000.000 MHz is outside the V3 tuner's range"]
    state.set_vfo(HIGH + 5000)
    assert len(sim.messages) == 1, "said once, not on every step"
    state.set_vfo(FM)
    state.set_vfo(HIGH)
    assert len(sim.messages) == 2
    state.cycle_tuner_pref(-1)                                       # AUTO
    assert state.tuner == "XTR"
    state.cycle_tuner_pref(2)                                        # XTR only, on HF: deaf, and said so
    state.set_vfo(HF)
    assert state.tuner == "XTR" and sim.messages[-1] == "7.200 MHz is outside the XTR tuner's range"


def test_one_dongle_simulator_stays_on_it(sim_one, state):   # noqa: F811
    assert not state.adsb_own_dongle and state.tuner == "RTL"
    for hz in (HF, FM, GAP, config.FREQ_MAX):
        state.set_vfo(hz)
        assert state.tuner == "RTL"
    del sim_one.messages[:]
    state.cycle_tuner_pref()
    assert state.tuner == "RTL" and sim_one.messages == []


def test_map_on_the_second_dongle_leaves_the_receiver_playing(sim, state, qapp, tmp_path):   # noqa: F811
    state.set_vfo(FM)
    state.set_recording(True)
    assert sim.start_adsb_mode() is None
    assert state.adsb_mode and not state.dongle_lent and not state.receiver_paused
    assert sim.running and state.recording and state.tuner == "V3"
    sim.poll_adsb()
    assert len(state.aircraft) >= 10
    sim.frames.clear()
    sim._tick()
    assert sim.frames, "the spectrum stopped although the receiver has its own dongle"
    state.set_vfo(146_520_000)                                       # the receiver can still be tuned
    assert sim.center_hz == pytest.approx(146_520_000 - 0.25 * config.SAMPLE_RATE)
    state.set_vfo(HF)                                                # HF too: it is on the V3 as well
    assert state.tuner == "V3 HF" and sim.running and not state.receiver_paused
    state.set_recording(False)
    assert sim.stop_adsb_mode() is None
    assert not state.adsb_mode and sim.running and state.tuner == "V3 HF"


def test_map_moves_the_receiver_off_the_xtr_when_the_main_tuner_can_take_over(sim, state):   # noqa: F811
    state.set_vfo(FM)
    state.cycle_tuner_pref(2)                                        # listening on the XTR by choice
    assert state.tuner == "XTR"
    assert sim.start_adsb_mode() is None
    assert state.tuner == "V3" and sim.running and not state.receiver_paused
    assert sim.stop_adsb_mode() is None
    assert state.tuner == "XTR", "the choice stands once the XTR is free again"


def test_map_pauses_the_receiver_when_it_was_on_what_only_the_xtr_tunes(sim, state):   # noqa: F811
    state.set_vfo(HIGH)
    assert sim.start_adsb_mode() is None
    assert state.adsb_mode and state.receiver_paused and state.dongle_lent and not sim.running
    assert state.tuner == "XTR"
    assert sim.stop_adsb_mode() is None
    assert sim.running and not state.receiver_paused and state.tuner == "XTR"


def test_tuning_above_the_v3_under_the_map_stays_on_it_until_the_map_closes(sim, state):   # noqa: F811
    state.set_vfo(FM)
    sim.start_adsb_mode()
    del sim.messages[:]
    state.set_vfo(HIGH)                                              # e.g. the record timer
    assert state.tuner == "V3"
    assert sim.messages == ["2000.000 MHz is outside the V3 tuner's range (the other tuner is on ADS-B)"]
    sim.stop_adsb_mode()
    assert state.tuner == "XTR"


def test_record_timer_runs_beside_the_map_unless_it_needs_the_xtr(sim, state, timer, clock):   # noqa: F811
    timer.must_wait = sim.waits_for_adsb
    arm(state, 20 * 60, length_min=30)
    state.arm_timer(False)
    state.set_vfo(HIGH)                                              # only the XTR has it
    state.arm_timer(True)
    state.set_vfo(FM)
    assert not sim.waits_for_adsb(HIGH)
    sim.start_adsb_mode()
    assert sim.waits_for_adsb(HIGH) and not sim.waits_for_adsb(FM) and not sim.waits_for_adsb(HF)
    assert not sim.waits_for_adsb(2_500_000_000), "nothing tunes that with or without the map"
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    assert not state.recording and state.vfo_hz == FM, "it would have recorded a deaf tuner"
    sim.stop_adsb_mode()
    clock.wall = utc(20, 5)
    timer.check(clock.wall)                                          # the rest of the window is still recorded
    assert state.recording and state.vfo_hz == HIGH and state.tuner == "XTR"
    state.set_recording(False)
    state.arm_timer(False)
    state.set_vfo(6_840_000)                                         # HF is the V3's: no waiting
    state.arm_timer(True)
    state.set_vfo(FM)
    sim.start_adsb_mode()
    clock.wall = utc(20, 0) + 86_400
    timer.check(clock.wall)
    assert state.recording and state.adsb_mode and state.vfo_hz == 6_840_000 and state.tuner == "V3 HF"
    state.set_recording(False)
    sim.stop_adsb_mode()


def test_a_decoder_that_fails_puts_the_receiver_back_where_it_was(sim, state, monkeypatch):   # noqa: F811
    monkeypatch.setattr(sim, "_hw_adsb", lambda on: "dump1090-fa stopped: no supported devices")
    state.set_vfo(FM)
    state.cycle_tuner_pref(2)
    assert sim.start_adsb_mode() == "dump1090-fa stopped: no supported devices"
    assert state.tuner == "XTR" and sim.running and not state.adsb_mode
    state.set_vfo(HIGH)
    assert sim.start_adsb_mode() == "dump1090-fa stopped: no supported devices"
    assert state.tuner == "XTR" and sim.running and not state.receiver_paused and not state.adsb_mode


# --------------------------------------------------------------------------- GNU Radio back end
# radio2 is the real radio's pair with the V3 as device 1 and the XTR as device 0.
def device_args(backend) -> str:
    return backend.src.kwargs["args"]


@needs_fake_gr
def test_each_frequency_opens_the_right_dongle_the_right_way(radio2, state):   # noqa: F811
    assert device_args(radio2) == "numchan=1 rtl=1,direct_samp=2" and state.tuner == "V3 HF"    # MW at start-up
    state.tune_to(FM, "WFM", 100_000)
    assert device_args(radio2) == "numchan=1 rtl=1" and state.tuner == "V3"
    assert open_sources() == 1 and len(fakegr.OPEN_SINKS) == 1, "the graph before it was never released"
    assert radio2.tb.running
    assert abs(radio2.src.last("set_center_freq")[0] - FM) == pytest.approx(0.25 * config.SAMPLE_RATE)
    assert radio2.sel_in.last("set_output_index") == (radio2._mode_index["WFM"],)
    state.set_vfo(HIGH)
    assert device_args(radio2) == "numchan=1 rtl=0" and state.tuner == "XTR"
    state.set_vfo(HF)
    assert device_args(radio2) == "numchan=1 rtl=1,direct_samp=2"
    state.set_vfo(GAP)
    assert device_args(radio2) == "numchan=1 rtl=1"
    state.set_vfo(1_300_000_000)
    assert device_args(radio2) == "numchan=1 rtl=1" and open_sources() == 1


@needs_fake_gr
def test_tuning_within_one_dongle_does_not_reopen_it(radio2, state):   # noqa: F811
    state.set_vfo(FM)
    src = radio2.src
    for hz in (30_000_000, 88_000_000, 446_000_000, 1_090_000_000, 1_700_000_000):
        state.set_vfo(hz)
    assert radio2.src is src


@needs_fake_gr
def test_gain_is_cut_to_what_the_new_tuner_has(radio2, state):   # noqa: F811
    state.set_vfo(FM)
    state.set_gain(49)
    state.set_vfo(HIGH)
    assert state.gain_db == config.XTR_GAIN_MAX_DB
    assert radio2.src.last("set_gain") == (config.XTR_GAIN_MAX_DB, 0)


@needs_fake_gr
def test_bias_tee_follows_the_v3(radio2, state, tmp_path, monkeypatch):   # noqa: F811
    log = tmp_path / "biast.args"
    monkeypatch.setenv("PATH", f"{script(tmp_path, 'rtl_biast', f'echo \"$@\" >> {log}').parent}:/usr/bin:/bin")
    state.set_vfo(FM)
    state.set_bias_tee(True)                                         # on the V3: reopened with the flag
    assert device_args(radio2) == "numchan=1 rtl=1,bias=1" and not log.exists()
    state.set_vfo(HIGH)                                              # the XTR never gets the flag
    assert device_args(radio2) == "numchan=1 rtl=0"
    src = radio2.src
    state.set_bias_tee(False)                                        # the V3 is idle: from outside
    assert radio2.src is src, "the XTR was reopened for a switch it does not have"
    state.set_bias_tee(True)
    assert log.read_text().splitlines() == ["-d 1 -b 0", "-d 1 -b 1"]
    state.set_vfo(HF)
    assert device_args(radio2) == "numchan=1 rtl=1,direct_samp=2,bias=1"


@needs_fake_gr
def test_bias_tee_left_on_is_switched_on_at_start_when_the_receiver_starts_on_the_xtr(state, monkeypatch, tmp_path):
    from pisdr.radio import flowgraph
    log = tmp_path / "biast.args"
    monkeypatch.setenv("PATH", f"{script(tmp_path, 'rtl_biast', f'echo \"$@\" >> {log}').parent}:/usr/bin:/bin")
    state.set_vfo(HIGH)
    state.set_bias_tee(True)
    backend = flowgraph.GnuRadioBackend(state, lineup=TWO_DONGLES)
    backend.start()
    backend._poll.stop()
    assert device_args(backend) == "numchan=1 rtl=0" and log.read_text().splitlines() == ["-d 1 -b 1"]
    backend.stop()


@needs_fake_gr
def test_adsb_runs_on_the_xtr_beside_the_receiver(radio2, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.tune_to(FM, "WFM", 100_000)
    state.set_recording(True)
    src = radio2.src
    assert radio2.start_adsb_mode() is None
    assert state.adsb_mode and not state.receiver_paused and state.recording
    assert radio2.src is src and radio2.tb.running and open_sources() == 1, "the receiver was disturbed"
    argv = args.read_text().split()
    assert argv[argv.index("--device") + 1] == "0", "the decoder was not sent to the XTR"
    radio2.poll_adsb()
    assert [ac.name for ac in state.aircraft][:2] == ["UAL123", "KLM1023"]
    state.set_vfo(146_520_000)                                       # still a radio
    assert abs(radio2.src.last("set_center_freq")[0] - 146_520_000) == pytest.approx(0.25 * config.SAMPLE_RATE)
    state.set_recording(False)
    del src
    state.set_vfo(HF)                                                # and an HF radio: that is the V3 too
    assert device_args(radio2) == "numchan=1 rtl=1,direct_samp=2" and radio2.tb.running and open_sources() == 1
    state.set_vfo(HIGH)                                              # the XTR is taken: stay on the V3
    assert device_args(radio2) == "numchan=1 rtl=1" and "on ADS-B" in radio2.messages[-1]
    process = radio2.adsb._proc
    assert radio2.stop_adsb_mode() is None
    assert process.poll() is not None and not state.adsb_mode
    assert device_args(radio2) == "numchan=1 rtl=0", "2 GHz did not come back with the XTR"
    assert open_sources() == 1 and radio2.tb.running


@needs_fake_gr
def test_adsb_takes_the_xtr_from_a_receiver_that_can_move_to_the_v3(radio2, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env)
    state.set_vfo(FM)
    state.cycle_tuner_pref(2)
    assert device_args(radio2) == "numchan=1 rtl=0"
    assert radio2.start_adsb_mode() is None
    assert device_args(radio2) == "numchan=1 rtl=1" and radio2.tb.running and not state.receiver_paused
    assert radio2.stop_adsb_mode() is None
    assert device_args(radio2) == "numchan=1 rtl=0"


@needs_fake_gr
def test_adsb_from_above_the_v3_pauses_the_receiver_and_gives_it_back(radio2, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.set_vfo(HIGH)
    assert radio2.start_adsb_mode() is None
    assert state.receiver_paused and open_sources() == 0, "the XTR is still open: the decoder cannot have it"
    assert "--device 0" in args.read_text()
    assert radio2.stop_adsb_mode() is None
    assert not state.receiver_paused and open_sources() == 1 and radio2.tb.running
    assert device_args(radio2) == "numchan=1 rtl=0"


@needs_fake_gr
def test_decoder_failing_on_the_xtr_leaves_the_receiver_as_it_was(radio2, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env, data=None, then='echo "usb_claim_interface error -6" >&2\nexit 1')
    state.set_vfo(FM)
    src = radio2.src
    assert radio2.start_adsb_mode() == "dump1090-fa stopped: usb_claim_interface error -6"
    assert radio2.src is src and radio2.tb.running and not state.adsb_mode
    del src
    state.set_vfo(HIGH)                                              # and the XTR is free to be used again
    assert device_args(radio2) == "numchan=1 rtl=0"
    assert radio2.start_adsb_mode() == "dump1090-fa stopped: usb_claim_interface error -6"
    assert open_sources() == 1 and radio2.tb.running and not state.receiver_paused and not radio2.radio_down


@needs_fake_gr
def test_one_dongle_is_not_named_to_the_decoder(radio, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    assert radio.start_adsb_mode() is None and state.receiver_paused
    assert "--device " not in args.read_text()
    radio.stop_adsb_mode()


@needs_fake_gr
def test_bias_tee_stays_with_the_v3_when_the_decoder_is_on_the_xtr(radio2, state, decoder_env, tmp_path):   # noqa: F811
    args = fake_decoder(decoder_env, help_text=MUTABILITY_HELP, name="dump1090-mutability")
    log = tmp_path / "biast.args"
    script(decoder_env, "rtl_biast", f'echo "$@" >> {log}\n')
    state.set_vfo(FM)
    state.set_bias_tee(True)
    assert device_args(radio2) == "numchan=1 rtl=1,bias=1"
    assert radio2.start_adsb_mode() is None
    assert "--device-index 0" in args.read_text()
    assert not log.exists(), "the XTR has no bias tee: nothing to switch for the decoder"
    assert device_args(radio2) == "numchan=1 rtl=1,bias=1"
    radio2.stop_adsb_mode()


def test_a_decoder_on_a_dongle_with_a_bias_tee_gets_it_by_index(decoder_env, tmp_path):   # noqa: F811
    fake_decoder(decoder_env, help_text=MUTABILITY_HELP, name="dump1090-mutability")
    log = tmp_path / "biast.args"
    script(decoder_env, "rtl_biast", f'echo "$@" >> {log}\n')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start(bias_tee=True, device=1) is None
    assert log.read_text().splitlines() == ["-d 1 -b 1"]
    decoder.stop()


def test_decoder_is_told_which_dongle_in_its_own_words(decoder_env):   # noqa: F811
    fa = str(script(decoder_env, "fa", f"cat <<'EOF'\n{FA_HELP}EOF\n"))
    old = str(script(decoder_env, "old", f"cat <<'EOF'\n{MUTABILITY_HELP}EOF\n"))
    bare = str(script(decoder_env, "bare", "echo '--write-json <dir>'\n"))
    assert adsb_decoder.build_command(fa, "/x", device=1)[:5] == [fa, "--device-type", "rtlsdr", "--device", "1"]
    assert adsb_decoder.build_command(old, "/x", device=0)[:3] == [old, "--device-index", "0"]
    assert "--device" not in adsb_decoder.build_command(fa, "/x")
    assert adsb_decoder.device_option(bare) is None
    assert "--device" not in adsb_decoder.build_command(bare, "/x", device=1)


def test_a_decoder_that_cannot_be_pointed_at_the_second_dongle_is_not_started(decoder_env):   # noqa: F811
    fake_decoder(decoder_env, help_text="--write-json <dir>\n")
    decoder = adsb_decoder.AdsbDecoder()
    assert "cannot be told which dongle" in decoder.start(device=1)
    assert not decoder.alive
    assert decoder.start(device=0) is None, "the first dongle is where it goes anyway"
    decoder.stop()


@needs_fake_gr
def test_network_mode_serves_the_dongle_the_receiver_is_on(radio2, state, tmp_path, monkeypatch):   # noqa: F811
    log = tmp_path / "args.txt"
    fake_rtl_tcp(tmp_path, monkeypatch, f'[ "$1" = "-h" ] && {{ echo "usage: -T bias"; exit 0; }}\n'
                                        f'echo "$@" > {log}\nexec sleep 30\n')
    state.set_vfo(FM)
    state.set_bias_tee(True)
    assert radio2.start_network_mode() is None and open_sources() == 0
    assert log.read_text().split() == ["-a", "0.0.0.0", "-p", str(config.RTL_TCP_PORT), "-d", "1", "-T"], \
        "the V3 is device 1 and has the bias tee"
    assert radio2.stop_network_mode() is None
    state.set_vfo(HIGH)
    assert radio2.start_network_mode() is None
    assert log.read_text().split() == ["-a", "0.0.0.0", "-p", str(config.RTL_TCP_PORT)], \
        "the XTR is device 0 and has no bias tee"
    assert radio2.stop_network_mode() is None and open_sources() == 1


@needs_fake_gr
def test_a_dongle_that_will_not_open_is_left_by_tuning_to_the_other(radio2, state, monkeypatch):   # noqa: F811
    import osmosdr
    real = osmosdr.source

    def xtr_missing(*args, **kwargs):
        if "rtl=0" in kwargs["args"]:
            raise RuntimeError("Failed to open rtlsdr device #0")
        return real(*args, **kwargs)
    monkeypatch.setattr(osmosdr, "source", xtr_missing)
    state.set_vfo(HIGH)
    assert radio2.radio_down and radio2.messages[-1].startswith("Radio did not reopen: Failed to open rtlsdr device #0")
    state.set_volume(10)                                             # nothing may raise meanwhile
    state.set_vfo(HIGH + 200_000)
    assert radio2.radio_down
    state.set_vfo(FM)
    assert not radio2.radio_down and radio2.tb.running and device_args(radio2) == "numchan=1 rtl=1"
    assert radio2.volume.last("set_k") == (0.1 ** 2,)


# --------------------------------------------------------------------------- on the screen
def test_the_tuner_in_use_is_on_the_vfo_bar(window, qapp):
    state = window.state
    state.set_vfo(FM)
    left = vfo_bar.bottom_indicators(state)[0]
    assert [i.key for i in left][:2] == ["band", "tuner"]
    assert (left[1].text, left[1].action) == ("V3", "settings:Tuner")
    state.set_vfo(HIGH)
    assert vfo_bar.bottom_indicators(state)[0][1].text == "XTR"
    state.set_vfo(HF)
    assert vfo_bar.bottom_indicators(state)[0][1].text == "V3 HF"
    bar = window.main_page.vfo_bar
    state.select_band(next(i for i, band in enumerate(state.bands) if band.name == "HF utility"))
    state.set_step_hz(12_500)
    state.set_gain(49)
    state.set_volume(100)
    bar.repaint()
    for key in ("band", "tuner", "step", "zoom", "knob", "volume", "gain"):
        assert bar.indicator_rect(key) is not None, f"no room left for {key}"
    for key in ("mode", "agc", "bias", "rec"):
        assert bar.indicator_rect(key) is not None, f"the tuner pushed {key} off the top line"
    window._open_named("settings:Tuner")
    page = window.settings_page
    assert window.stack.currentIndex() == window.PAGE_SETTINGS and page.rows[page.selected].label == "Tuner"


def test_settings_row_chooses_the_tuner(window, qapp):
    state, page = window.state, window.settings_page
    state.set_vfo(FM)
    window.show_page(window.PAGE_SETTINGS)
    row = next(r for _, r in rows(window) if r.label == "Tuner")
    assert row.get() == "AUTO \u00b7 V3"
    row.adjust(0)
    assert state.tuner_pref == "MAIN" and row.get() == "V3 only"
    row.adjust(1)
    assert state.tuner_pref == "AUX" and state.tuner == "XTR" and row.get() == "XTR only"
    window.backend.start_adsb_mode()
    assert row.get() == "XTR only \u00b7 V3", "it does not say the receiver is on the XTR when it is not"
    window.backend.stop_adsb_mode()
    row.adjust(1)
    assert row.get() == "AUTO \u00b7 V3"
    page._refresh_info()
    assert "V3 + XTR (simulated)" in page.info.text()


def test_settings_row_with_one_dongle_only_names_it(one_dongle_window, qapp):
    window = one_dongle_window
    window.show_page(window.PAGE_SETTINGS)
    row = next(r for _, r in rows(window) if r.label == "Tuner")
    assert row.get() == "RTL"
    row.adjust(0)
    assert window.state.tuner_pref == "AUTO" and window.toast.text() == "Only one tuner is attached"


def test_rf_slider_spans_the_gain_of_the_tuner_in_use(window, qapp):
    state = window.state
    level = sliders.levels(state)["GAIN"]
    state.set_vfo(HIGH)
    level.set_fraction(1.0)
    assert state.gain_db == pytest.approx(config.XTR_GAIN_MAX_DB, abs=config.GAIN_STEP_DB)
    assert level.fraction() == pytest.approx(1.0, abs=0.03)
    state.set_vfo(FM)
    assert level.fraction() < 0.9, "42 dB is not the top of the V3's range"
    level.set_fraction(1.0)
    assert state.gain_db == pytest.approx(config.GAIN_MAX_DB, abs=config.GAIN_STEP_DB)
    pump(qapp, 0.02)
