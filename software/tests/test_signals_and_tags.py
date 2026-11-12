"""Signal detection (markers, snap, seek), frequency tags and the Morse decoder."""
from __future__ import annotations

import json

import numpy as np
import pytest

from pisdr import config, signals, tags
from pisdr.memories import Memory
from pisdr.radio.backend_sim import _sim_cw_pattern
from pisdr.radio.cw_decoder import MorseDecoder

N = config.FFT_SIZE
START_HZ = 145_000_000.0
BIN_HZ = 1875.0                     # 1.92 MHz / 1024


def spectrum(*carriers, floor=-100.0, tilt_db=0.0, seed=1) -> np.ndarray:
    """Noise floor (optionally sloping) plus (bin, level_db, width_bins) humps."""
    rng = np.random.default_rng(seed)
    x = np.arange(N)
    local_floor = floor + tilt_db * x / N
    db = local_floor + rng.normal(0.0, 1.5, N)
    for centre, level, width in carriers:
        shape = np.exp(-0.5 * ((x - centre) / (width / 2.355)) ** 2)
        db = np.maximum(db, local_floor + (level - local_floor) * shape)
    return db


def hz(bin_index: float) -> float:
    return START_HZ + (bin_index + 0.5) * BIN_HZ


# --------------------------------------------------------------------------- detection
def test_finds_each_signal_strongest_first():
    found = signals.detect(spectrum((200, -60, 6), (500, -40, 6), (800, -75, 6)), START_HZ, BIN_HZ)
    assert [round((s.peak_hz - START_HZ) / BIN_HZ - 0.5) for s in found] == [500, 200, 800]
    assert found[0].snr_db > found[1].snr_db > found[2].snr_db


def test_peak_is_located_to_a_fraction_of_a_bin():
    (sig,) = signals.detect(spectrum((300.3, -50, 5)), START_HZ, BIN_HZ)
    assert abs(sig.peak_hz - hz(300.3)) < 0.25 * BIN_HZ
    assert sig.lo_hz < sig.peak_hz < sig.hi_hz


@pytest.mark.parametrize("seed", range(8))
def test_plain_noise_has_no_signals(seed):
    assert signals.detect(spectrum(seed=seed), START_HZ, BIN_HZ) == []


@pytest.mark.parametrize("tilt", [-30.0, 30.0])
def test_sloping_noise_floor_is_not_a_signal(tilt):
    """HF noise rises steeply towards low frequencies; that slope must not get a marker."""
    assert signals.detect(spectrum(tilt_db=tilt), START_HZ, BIN_HZ) == []
    found = signals.detect(spectrum((600, -55, 6), tilt_db=tilt), START_HZ, BIN_HZ)
    assert len(found) == 1 and abs(found[0].peak_hz - hz(600)) < BIN_HZ


def test_wide_station_is_one_signal_not_many():
    found = signals.detect(spectrum((512, -45, 110)), START_HZ, BIN_HZ)       # FM broadcast
    assert len(found) == 1
    assert abs(found[0].centroid_hz - hz(512)) < 3 * BIN_HZ
    assert found[0].width_hz > 100 * BIN_HZ


def test_occupied_bandwidth_ignores_the_skirts():
    x = np.arange(N)
    db = np.full(N, -100.0) + np.random.default_rng(3).normal(0, 1.0, N)
    db[np.abs(x - 400) <= 3] = -40.0            # 7 bins flat = 13 kHz
    db[np.abs(x - 400) == 4] = -70.0            # weak skirts either side
    (sig,) = signals.detect(db, START_HZ, BIN_HZ)
    assert sig.width_hz >= 9 * BIN_HZ
    assert 6 * BIN_HZ <= sig.occupied_hz <= 8 * BIN_HZ


def test_tiny_and_degenerate_frames():
    assert signals.detect(np.array([]), 0.0, 1.0) == []
    assert signals.detect(np.full(4, -50.0), 0.0, 1.0) == []
    assert signals.detect(np.full(N, -100.0), START_HZ, BIN_HZ) == []          # dead flat
    assert signals.detect(np.full(N, -300.0), START_HZ, BIN_HZ) == []          # paused FFT


# --------------------------------------------------------------------------- where to tune
def sig(lo, hi, peak=None, centroid=None, occupied=None) -> signals.Signal:
    mid = (lo + hi) / 2
    return signals.Signal(lo, hi, peak or mid, centroid or mid, -40.0, 30.0, occupied or (hi - lo))


def test_fm_snaps_to_the_channel_grid_when_close():
    s = sig(146_514_000, 146_527_000, centroid=146_520_400)
    assert signals.tune_target(s, "NFM", 5000, 500.0) == 146_520_000


def test_off_grid_signal_is_tuned_exactly():
    s = sig(146_517_000, 146_519_000, centroid=146_517_800)
    assert signals.tune_target(s, "NFM", 25_000, 100.0) == 146_517_800


def test_am_tunes_the_carrier_not_the_middle_of_the_sidebands():
    s = sig(7_196_000, 7_206_000, peak=7_200_020, centroid=7_201_500)
    assert signals.tune_target(s, "AM", 10, 50.0) == 7_200_020


def test_ssb_tunes_to_the_suppressed_carrier():
    s = sig(14_200_300, 14_202_700)
    assert signals.tune_target(s, "USB", 100, 50.0) == 14_200_000
    assert signals.tune_target(s, "LSB", 100, 50.0) == 14_203_000
    assert signals.tune_target(s, "USB", 100, 1875.0) is None       # view too coarse to see edges


def test_cw_tunes_the_tone():
    assert signals.tune_target(sig(7_030_090, 7_030_150, peak=7_030_123), "CW", 100, 30.0) == 7_030_120


def test_filter_is_only_fitted_when_the_width_can_be_measured():
    assert signals.fit_width(sig(0, 12_000, occupied=11_000), "NFM", 500.0) == 11_000
    assert signals.fit_width(sig(0, 4_000, occupied=2_000), "NFM", 1875.0) is None   # ~1 bin wide
    assert signals.fit_width(sig(0, 200_000), "WFM", 1875.0) is None                 # one WFM filter


def test_signal_under_the_tap_wins_over_a_near_miss():
    a, b = sig(1000, 2000), sig(2300, 2600)
    assert signals.signal_at([a, b], 1500, 400) is a
    assert signals.signal_at([a, b], 2200, 400) is b
    assert signals.signal_at([a, b], 5000, 400) is None


def test_seek_moves_to_the_next_signal_and_skips_the_current_one():
    found = [sig(100_000, 110_000), sig(200_000, 210_000), sig(300_000, 310_000)]
    args = ("NFM", 5000, 500.0)
    up = signals.seek(found, 205_000, +1, *args)
    down = signals.seek(found, 205_000, -1, *args)
    assert (up[1], down[1]) == (305_000, 105_000)
    assert signals.seek(found, 305_000, +1, *args) is None
    assert signals.seek([], 0, +1, *args) is None


# --------------------------------------------------------------------------- tags
@pytest.fixture
def store(state):
    return tags.TagStore(state)


def test_builtin_tags_are_listed_in_frequency_order(store):
    found = store.tags_between(2_000_000, 30_000_000)
    assert len(found) > 10
    assert [t.hz for t in found] == sorted(t.hz for t in found)
    assert store.label_near(10_000_000, 500) != ""


def test_memory_name_beats_user_tag_beats_builtin(store, state):
    builtin = store.tags_between(9_999_000, 10_001_000)[0]
    assert builtin.source == "builtin"
    store.user = [tags.Tag(builtin.hz, "MINE", "user")]
    assert store.tags_between(9_999_000, 10_001_000)[0].label == "MINE"
    state.memories = [Memory("Time signal", builtin.hz, "AM", 1000)]
    assert store.tags_between(9_999_000, 10_001_000)[0].label == "Time signal"


def test_station_name_is_learned_from_stable_rds(store, state):
    state.tune_to(94_900_000, "WFM", 100_000)
    state.set_rds("ps", "KUOW")
    assert store._rds_timer.isActive()
    store._check_rds()                                      # the 5 s timer firing
    assert store.label_near(94_900_000, 1000) == "KUOW"
    saved = json.loads(config.USER_TAGS_FILE.read_text())["tags"]
    assert saved == [{"hz": 94_900_000, "label": "KUOW", "src": "rds"}]
    assert tags.TagStore(state).label_near(94_900_000, 1000) == "KUOW"      # survives a restart


def test_scrolling_rds_text_is_not_learned(store, state):
    state.tune_to(94_900_000, "WFM", 100_000)
    state.set_rds("ps", "NOW PLAY")
    state.set_rds("ps", "ING: SON")                         # changed before the timer fired
    state.rds_ps = "G TITLE"
    store._check_rds()
    assert store.user == []


def test_station_name_is_not_learned_after_tuning_away(store, state):
    state.tune_to(94_900_000, "WFM", 100_000)
    state.set_rds("ps", "KUOW")
    state.set_vfo(98_100_000)
    store._check_rds()
    assert store.user == []


def test_hand_written_tag_is_never_replaced_by_rds(store, state):
    store.user = [tags.Tag(94_900_000, "My station", "user")]
    state.tune_to(94_900_000, "WFM", 100_000)
    state.set_rds("ps", "KUOW")
    store._check_rds()
    assert store.label_near(94_900_000, 1000) == "My station"


def test_damaged_tag_files_are_ignored(state):
    config.USER_TAGS_FILE.write_text('{"tags": [{"hz": "x", "label": "bad"}, 7, null, '
                                     '{"hz": 1e400, "label": "huge"}, {"hz": 5000000, "label": "ok"}]}')
    assert [t.label for t in tags.TagStore(state).user] == ["ok"]
    config.USER_TAGS_FILE.write_text("{ not json")
    assert tags.TagStore(state).user == []


# --------------------------------------------------------------------------- Morse
RATE = config.CW_ENVELOPE_RATE


def band_noise(seconds: float, seed: int, level: float = 0.05) -> np.ndarray:
    """What the decoder really gets with no signal: the envelope of noise through the
    narrow CW filter. Rayleigh distributed and smooth over ~10 ms, so bursts look like dits."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    noise = rng.normal(size=n) + 1j * rng.normal(size=n)
    half = RATE // 100
    taps = np.hanning(2 * half + 1)
    taps /= np.sqrt((taps ** 2).sum())
    return np.convolve(noise, taps, "same") * level


def keyed(text: str, wpm: int, snr_db: float = 20.0, seed: int = 0, lead_s: float = 1.0) -> np.ndarray:
    """Envelope of *text* sent at *wpm* in band noise, *snr_db* above it."""
    pattern = _sim_cw_pattern(text, wpm, RATE)
    signal = np.concatenate([np.zeros(int(lead_s * RATE)), pattern, np.zeros(RATE)])
    amplitude = 0.05 * np.sqrt(2) * 10 ** (snr_db / 20)
    return np.abs(signal * amplitude + band_noise(len(signal) / RATE, seed)[:len(signal)])


def decode(env: np.ndarray, chunk: int = 37) -> str:
    out: list[str] = []
    decoder = MorseDecoder(out.append)
    for i in range(0, len(env), chunk):                      # arbitrary block sizes, like GNU Radio
        decoder.process(env[i:i + chunk])
    return "".join(out).strip()


MESSAGE = "CQ CQ DE W1AW W1AW K"


@pytest.mark.parametrize("wpm", [10, 15, 20, 25, 30])
def test_morse_is_decoded_across_speeds(wpm):
    # The first word may be lost while the decoder learns the speed; send it twice.
    text = decode(keyed(f"{MESSAGE} {MESSAGE}", wpm))
    assert text.endswith(MESSAGE), text


def test_morse_speed_is_reported():
    out: list[str] = []
    decoder = MorseDecoder(out.append)
    decoder.process(keyed(f"{MESSAGE} {MESSAGE}", 24))
    assert 21 <= decoder.wpm <= 27


@pytest.mark.parametrize("chunk", [1, 7, 256, 4096])
def test_morse_result_does_not_depend_on_block_size(chunk):
    env = keyed(f"{MESSAGE} {MESSAGE}", 18)
    assert decode(env, chunk).endswith(MESSAGE)


@pytest.mark.parametrize("seed", range(6))
def test_static_alone_prints_almost_nothing(seed):
    """The complaint this decoder was reworked for: a stream of letters with no signal there."""
    text = decode(np.abs(band_noise(60, seed)))
    assert len(text.replace(" ", "")) <= 2, f"{text!r} from a minute of noise"


@pytest.mark.parametrize("seed", range(4))
def test_static_before_a_signal_does_not_throw_the_speed_off(seed):
    text = decode(keyed(f"{MESSAGE} {MESSAGE}", 22, seed=seed, lead_s=45.0))
    assert text.endswith(f"{MESSAGE} {MESSAGE}"), text


def test_words_starting_with_e_or_t_are_kept():
    """A lone dit is treated as static, but only when nothing follows it."""
    text = decode(keyed("CQ TEST ES TU EE 73 CQ TEST ES TU EE 73", 20))
    assert text.endswith("CQ TEST ES TU EE 73"), text


def test_fading_does_not_break_decoding():
    env = keyed(f"{MESSAGE} {MESSAGE} {MESSAGE}", 20)
    fade = 0.6 + 0.4 * np.sin(np.arange(len(env)) / RATE * 2 * np.pi / 7.0)
    assert MESSAGE in decode(env * fade)


def test_locked_speed_is_not_changed_by_what_is_heard():
    out: list[str] = []
    decoder = MorseDecoder(out.append, wpm_lock=20)
    decoder.set_wpm_lock(20)
    decoder.process(keyed(f"{MESSAGE} {MESSAGE}", 20))
    assert decoder.wpm == pytest.approx(20) and "".join(out).strip().endswith(MESSAGE)


def test_fast_percentile_matches_numpy():
    rng = np.random.default_rng(5)
    for size in (1, 2, 3, 5, 16, 25, 26, 51, 103, 1024):
        for _ in range(20):
            values = rng.normal(-100.0, 8.0, size)
            assert signals._percentile_20(values) == pytest.approx(np.percentile(values, 20), abs=1e-9)
    assert signals._percentile_20(np.full(7, -90.0)) == -90.0
