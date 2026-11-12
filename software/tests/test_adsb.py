"""ADS-B map: the aircraft.json reader, map geometry, outlines, the decoder process, the
dongle hand-over on both back ends, and the page itself.

The decoder tests use a small shell script standing in for dump1090. The last test runs a
real dump1090 on a synthetic recording (``adsb_signal.py``) when one is installed, or when
``PISDR_TEST_DUMP1090`` names one or more binaries (separated by ``:``).
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import stat
import time
import types
from pathlib import Path

import numpy as np
import pytest

import adsb_signal
from conftest import needs_fake_gr, pump
from pisdr import adsb, config, outlines
from pisdr.adsb import Aircraft
from pisdr.qt import QtCore
from pisdr.radio import adsb_decoder
from pisdr.state import RadioState
from pisdr.ui import adsb_page
from test_radio import open_sources, radio, sim, write_wav  # noqa: F401  (radio and sim are fixtures)
from test_scan_and_timer import arm, timer, utc  # noqa: F401  (timer is a fixture)
from test_state import assert_sane
from test_ui import image_stats, press, rows, tap

HOME = (43.04, -87.91)

# What dump1090-fa / readsb write ...
FA_JSON = {"now": 1_700_000_000.0, "messages": 4521, "aircraft": [
    {"hex": "a1b2c3", "flight": "UAL123  ", "alt_baro": 35000, "alt_geom": 35600, "gs": 451.6, "track": 273.0,
     "baro_rate": -1216, "squawk": "1200", "lat": 43.2, "lon": -88.05, "seen_pos": 0.4, "messages": 468,
     "seen": 0.1, "rssi": -7.2},
    {"hex": "40621d", "flight": "KLM1023 ", "alt_baro": "ground", "gs": 12.0, "track": 45.0, "lat": 43.85,
     "lon": -87.1, "seen_pos": 2.0, "seen": 0.5, "rssi": -20.1, "messages": 40},
    {"hex": "abcdef", "seen": 3.2, "messages": 5, "rssi": -30.0},
    {"hex": "~c0ffee", "alt_baro": 4000, "seen": 1.0},
]}
# ... and what dump1090-mutability writes for the same sky.
MUTABILITY_JSON = {"now": 1_700_000_000.0, "messages": 4519, "aircraft": [
    {"hex": "a1b2c3", "flight": "UAL123  ", "altitude": 35000, "vert_rate": -1216, "speed": 451, "track": 274,
     "squawk": "1200", "lat": 43.2, "lon": -88.05, "seen_pos": 0.4, "messages": 467, "seen": 0.1, "rssi": -7.1},
    {"hex": "40621d", "flight": "KLM1023 ", "altitude": "ground", "speed": 12, "track": 45, "lat": 43.85,
     "lon": -87.1, "seen_pos": 2.0, "seen": 0.5, "rssi": -20.1, "messages": 40},
    {"hex": "abcdef", "seen": 3.2, "messages": 5, "rssi": -30.0},
]}

FA_HELP = ("--device-type <type>  Select SDR type\n--device <index|serial>\n--ppm <correction>\n"
           "--lat <latitude>\n--lon <longitude>\n--write-json <dir>\n--write-json-every <t>\n--quiet\n")
MUTABILITY_HELP = "--device-index <index>\n--ppm <error>\n--lat <latitude>\n--lon <longitude>\n" \
                  "--write-json <dir>\n--write-json-every <t>\n--quiet\n"
READSB_HELP = FA_HELP + "--enable-biastee  Set bias tee supply on (default: off)\n"


# --------------------------------------------------------------------------- aircraft.json
@pytest.mark.parametrize("data", (FA_JSON, MUTABILITY_JSON), ids=("fa", "mutability"))
def test_both_json_dialects_give_the_same_aircraft(data):
    aircraft, messages = adsb.parse_aircraft_json(json.dumps(data))
    assert messages == data["messages"]
    united, klm, quiet = aircraft[:3]
    assert (united.icao, united.callsign, united.name) == ("A1B2C3", "UAL123", "UAL123")
    assert (united.lat, united.lon, united.altitude_ft, united.vert_rate_fpm) == (43.2, -88.05, 35000, -1216)
    assert united.speed_kt == pytest.approx(451, abs=1) and united.track_deg == pytest.approx(273.5, abs=1)
    assert united.squawk == "1200" and united.has_position and not united.on_ground
    assert klm.on_ground and klm.altitude_ft is None and klm.has_position
    assert (quiet.name, quiet.callsign, quiet.has_position, quiet.altitude_ft) == ("ABCDEF", "", False, None)
    assert quiet.speed_kt is None and quiet.track_deg is None and quiet.squawk == ""


def test_non_icao_addresses_keep_their_marker():
    aircraft, _ = adsb.parse_aircraft_json(json.dumps(FA_JSON))
    assert aircraft[3].icao == "~C0FFEE" and aircraft[3].altitude_ft == 4000


def test_damaged_entries_are_dropped_one_by_one():
    entries = [
        {"hex": "111111", "seen": 1, "lat": "43.1", "lon": None, "alt_baro": 1e9, "gs": -5, "track": 720,
         "squawk": "9999", "flight": 17, "baro_rate": [1], "rssi": "loud", "messages": -4},
        {"hex": "222222", "lat": 10.0, "lon": 20.0, "seen_pos": config.ADSB_POSITION_MAX_AGE_S + 1, "seen": 2.0},
        {"hex": "333333", "seen": config.ADSB_MAX_AGE_S + 1},          # not heard for too long
        {"hex": "444444", "seen": True},
        {"hex": "zzzzzz"}, {"hex": "12345"}, {"hex": 0x123456}, {"flight": "NOHEX"}, "junk", None, 7, [],
        {"hex": "555555", "lat": float("nan"), "lon": 181.0, "flight": "  n1 2-3\u00e9 "},
    ]
    aircraft, messages = adsb.parse_aircraft_json(json.dumps({"aircraft": entries, "messages": "many"}))
    assert [ac.icao for ac in aircraft] == ["111111", "222222", "555555"] and messages == 0
    bad = aircraft[0]
    assert not bad.has_position and bad.callsign == "" and bad.squawk == "" and bad.messages == 0
    assert bad.altitude_ft is bad.speed_kt is bad.track_deg is bad.vert_rate_fpm is bad.rssi_db is None
    assert not aircraft[1].has_position, "a position a minute old is no longer where the aircraft is"
    assert aircraft[2].callsign == "N123" and not aircraft[2].has_position


@pytest.mark.parametrize("text", ("", "[]", "null", '{"aircraft": 5}', '{"now": 1}', "{truncated"))
def test_a_file_that_is_not_an_aircraft_list_is_refused(text):
    with pytest.raises(ValueError):
        adsb.parse_aircraft_json(text)


# --------------------------------------------------------------------------- geometry
@pytest.mark.parametrize("a, b, distance, bearing", (
    ((43.04, -87.91), (41.9786, -87.9048), 63.7, 179.8),          # Milwaukee -> Chicago O'Hare
    ((51.4775, -0.4614), (40.6413, -73.7781), 2991.0, 287.9),     # Heathrow -> JFK
    ((0.0, 179.5), (0.0, -179.5), 60.04, 90.0),                   # across the date line
    ((-33.9, 151.2), (-33.9, 151.2), 0.0, 0.0),
))
def test_distance_and_bearing(a, b, distance, bearing):
    d, brg = adsb.distance_bearing(*a, *b)
    assert d == pytest.approx(distance, rel=2e-3, abs=0.01)
    assert d == 0 or brg == pytest.approx(bearing, abs=0.2)


def test_projection_keeps_distance_and_direction_from_the_centre():
    lats = np.array([43.04, 44.04, 43.04, 40.0])
    lons = np.array([-87.91, -87.91, -86.91, -95.0])
    east, north = adsb.project(*HOME, lats, lons)
    assert east[0] == pytest.approx(0, abs=1e-6) and north[0] == pytest.approx(0, abs=1e-6)
    assert north[1] == pytest.approx(60.04, abs=0.05) and east[1] == pytest.approx(0, abs=1e-6)
    assert east[2] == pytest.approx(60.04 * math.cos(math.radians(43.04)), abs=0.1) and 0 < north[2] < 0.5
    for i in range(4):
        d, brg = adsb.distance_bearing(*HOME, lats[i], lons[i])
        assert math.hypot(east[i], north[i]) == pytest.approx(d, abs=1e-6)
    assert [adsb.compass_point(b) for b in (0, 22.4, 22.6, 90, 181, 270, 337.6, 359.9)] == \
        ["N", "N", "NE", "E", "S", "W", "N", "N"]


def test_sorting_and_the_middle_of_the_traffic():
    near, far, lost = Aircraft("A", lat=43.1, lon=-87.9), Aircraft("B", lat=44.5, lon=-87.9), Aircraft("C")
    fleet = [lost, far, near]
    adsb.locate(fleet, *HOME)
    assert [ac.icao for ac in adsb.by_distance(fleet)] == ["A", "B", "C"]
    assert lost.distance_nm is None and near.distance_nm == pytest.approx(3.6, abs=0.1)
    assert adsb.middle_of([lost]) is None
    lat, lon = adsb.middle_of([Aircraft("D", lat=10.0, lon=179.0), Aircraft("E", lat=12.0, lon=-179.0)])
    assert lat == pytest.approx(11.0) and abs(lon) == pytest.approx(180.0, abs=1e-6)


def test_where_the_receiver_is(state):
    assert adsb.receiver_position(state) is None
    state.space_wx = types.SimpleNamespace(wx={"lat": 51.5, "lon": -0.1})
    assert adsb.receiver_position(state) == (51.5, -0.1, "ip")
    state.space_wx = types.SimpleNamespace(wx={"lat": "north", "lon": 400})
    assert adsb.receiver_position(state) is None
    state.wx_lat, state.wx_lon = HOME
    assert adsb.receiver_position(state) == (*HOME, "settings")


def test_exactly_one_shipped_band_is_the_adsb_channel(state):
    names = [band.name for band in state.bands if adsb.is_adsb_band(band)]
    assert names == ["ADS-B 1090"]
    wide = config.Band("UHF", 400_000_000, 1_700_000_000, 1_090_000_000, "AM", 25_000)
    assert not adsb.is_adsb_band(wide)


def test_simulated_traffic_moves_and_reads_like_the_real_thing():
    traffic = adsb.SimTraffic(*HOME, now=0.0)
    first, messages = traffic.aircraft(now=10.0)
    later, more = traffic.aircraft(now=70.0)
    assert len(first) == len(later) >= 10 and more > messages > 0
    assert len({ac.icao for ac in first}) == len(first)
    assert any(not ac.has_position for ac in first) and any(ac.on_ground for ac in first)
    moved = 0
    for before, after in zip(first, later):
        assert before.icao == after.icao
        if not before.has_position or before.on_ground:
            continue
        flown, heading = adsb.distance_bearing(before.lat, before.lon, after.lat, after.lon)
        if flown < 100:                                   # (one may have wrapped to the start of its line)
            assert flown == pytest.approx(before.speed_kt / 60.0, rel=0.03)
            assert abs((heading - before.track_deg + 180) % 360 - 180) < 2.0
            moved += 1
    assert moved >= 8


# --------------------------------------------------------------------------- settings
def test_map_range_and_labels_step_stop_and_persist(state):
    ranges = [nm for nm, _ring in config.ADSB_RANGES_NM]
    assert state.adsb_range_nm == config.ADSB_DEFAULT_RANGE_NM and state.adsb_labels == "FULL"
    for _ in range(10):
        state.step_adsb_range(+1)
    assert state.adsb_range_nm == ranges[-1]
    for _ in range(10):
        state.step_adsb_range(-1)
    assert state.adsb_range_nm == ranges[0]
    seen = []
    for _ in range(len(config.ADSB_LABELS)):
        state.cycle_adsb_labels()
        seen.append(state.adsb_labels)
    assert sorted(seen) == sorted(config.ADSB_LABELS) and state.adsb_labels == "FULL"
    state.cycle_adsb_labels()
    state.save()
    again = RadioState()
    again.load()
    assert (again.adsb_range_nm, again.adsb_labels) == (ranges[0], "NAME")
    assert not again.adsb_mode and again.aircraft == [], "ADS-B mode never survives a restart"


@pytest.mark.parametrize("bad", ({"adsb_range_nm": 33, "adsb_labels": "LOUD"},
                                 {"adsb_range_nm": "100", "adsb_labels": None},
                                 {"adsb_range_nm": [], "adsb_labels": 2}))
def test_bad_map_settings_fall_back_to_defaults(bad):
    config.SETTINGS_FILE.write_text(json.dumps(dict(bad, volume=41)))
    state = RadioState()
    state.load()
    assert (state.adsb_range_nm, state.adsb_labels) == (config.ADSB_DEFAULT_RANGE_NM, "FULL")
    assert state.volume == 41
    assert_sane(state)


# --------------------------------------------------------------------------- outlines
def test_bundled_outlines_cover_coasts_lakes_and_borders():
    data = outlines.load()
    assert data is not None, "config/outlines.npz is missing - run tools/build_outlines.py"
    assert len(data["kind"]) > 2000 and set(np.unique(data["kind"])) == {0, 1, 2, 3}
    assert data["lat"].dtype == np.int16 and abs(data["lat"]).max() <= 9000 and abs(data["lon"]).max() <= 18000

    def kinds(lat, lon, radius=100.0):
        runs = outlines.near(lat, lon, radius)
        for _kind, east, north in runs:
            assert len(east) == len(north) >= 2
            assert np.hypot(east, north).min() <= radius, "a run that never enters the view"
        return {kind for kind, _e, _n in runs}

    assert outlines.LAKE in kinds(*HOME) and outlines.STATE in kinds(*HOME)        # Lake Michigan, WI / IL
    assert {outlines.COAST, outlines.BORDER} <= kinds(51.0, 2.0)                  # Channel, France / Belgium
    assert outlines.COAST in kinds(-17.8, 179.9) and outlines.COAST in kinds(-16.8, -179.9)   # Fiji
    assert kinds(0.0, -140.0) == set()                                             # open Pacific
    assert outlines.COAST in kinds(78.2, 15.6) and outlines.COAST in kinds(-77.8, 166.7)      # near the poles


def test_outlines_are_placed_where_they_are():
    """The southern tip of Lake Michigan is about 85 nm south-south-east of Milwaukee."""
    runs = [(east, north) for kind, east, north in outlines.near(*HOME, 150.0) if kind == outlines.LAKE]
    east = np.concatenate([e for e, _n in runs])
    north = np.concatenate([n for _e, n in runs])
    tip = int(np.argmin(np.where(np.abs(east) < 40, north, np.inf)))
    assert -95 < north[tip] < -75 and 0 < east[tip] < 40


def test_missing_or_damaged_outlines_just_leave_the_map_bare(tmp_path):
    assert outlines.near(*HOME, 100.0, path=tmp_path / "none.npz") == []
    (tmp_path / "junk.npz").write_bytes(b"not a zip file")
    assert outlines.near(*HOME, 100.0, path=tmp_path / "junk.npz") == []
    np.savez(tmp_path / "short.npz", lat=np.zeros(3, np.int16), lon=np.zeros(2, np.int16),
             start=np.array([0, 3]), kind=np.zeros(1, np.uint8), box=np.zeros((1, 4), np.int16))
    assert outlines.near(*HOME, 100.0, path=tmp_path / "short.npz") == []


def test_outline_build_tool(tmp_path, capsys):
    tool = Path(__file__).resolve().parent.parent / "tools" / "build_outlines.py"
    spec = importlib.util.spec_from_file_location("build_outlines", tool)
    build_outlines = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_outlines)

    def geojson(geometry) -> str:
        return json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": geometry}, {"type": "Feature", "geometry": None}]})
    source = tmp_path / "ne"
    source.mkdir()
    shore = [[-88.0, 42.5], [-87.8, 43.0], [-87.8, 43.0], [-87.9, 43.5]]          # one repeated point
    (source / "ne_50m_coastline.geojson").write_text(geojson({"type": "LineString", "coordinates": shore}))
    (source / "ne_50m_lakes.geojson").write_text(geojson({"type": "MultiPolygon", "coordinates": [
        [[[-87.0, 43.0], [-86.5, 43.0], [-86.5, 43.4], [-87.0, 43.0]]]]}))
    (source / "ne_50m_admin_0_boundary_lines_land.geojson").write_text(geojson(
        {"type": "MultiLineString", "coordinates": [[[10.0, 50.0], [11.0, 51.0]], [[12.0, 50.0]]]}))
    (source / "ne_50m_admin_1_states_provinces_lines.geojson").write_text(geojson(
        {"type": "LineString", "coordinates": [[-90.0, 42.5], [-87.0, 42.5]]}))
    out = tmp_path / "out.npz"
    assert build_outlines.main(["--source", str(source), "--out", str(out)]) == 0
    assert "4 lines, 11 points" in capsys.readouterr().out
    runs = outlines.near(*HOME, 60.0, path=out)
    assert sorted(kind for kind, _e, _n in runs) == [outlines.COAST, outlines.LAKE, outlines.STATE]
    coast = next((east, north) for kind, east, north in runs if kind == outlines.COAST)
    assert len(coast[0]) == 3
    assert coast[1][1] == pytest.approx(-2.4, abs=0.1) and coast[0][1] == pytest.approx(4.8, abs=0.1)


# --------------------------------------------------------------------------- decoder process
@pytest.fixture
def decoder_env(tmp_path, monkeypatch):
    """Fresh PATH entry for stand-in programs, scratch files under tmp_path, short start-up wait."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(adsb_decoder, "_HELP", {})
    monkeypatch.setattr(adsb_decoder, "_START_WAIT_S", 0.3)
    for name in config.ADSB_PROGRAMS:
        assert shutil.which(name) in (None, str(bin_dir / name)), f"a real {name} would be started"
    return bin_dir


def script(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_decoder(bin_dir: Path, help_text: str = FA_HELP, data: dict | None = FA_JSON,
                 then: str = "exec sleep 30", name: str = "dump1090-fa") -> Path:
    """A stand-in decoder: answers --help, logs its arguments, writes aircraft.json, then *then*."""
    args = bin_dir.parent / f"{name}.args"
    sample = bin_dir.parent / f"{name}.json"
    sample.write_text(json.dumps(data) if data is not None else "")
    script(bin_dir, name,
           f'if [ "$1" = "--help" ]; then cat <<\'EOF\'\n{help_text}EOF\nexit 0; fi\n'
           f'echo "$@" > {args}\n'
           'while [ $# -gt 0 ]; do [ "$1" = "--write-json" ] && dir="$2"; shift; done\n'
           + (f'cp {sample} "$dir/aircraft.json"\n' if data is not None else "")
           + then + "\n")
    return args


def test_command_line_uses_only_what_the_decoder_offers(decoder_env):
    fa = str(script(decoder_env, "fa", f"cat <<'EOF'\n{FA_HELP}EOF\n"))
    old = str(script(decoder_env, "old", f"cat <<'EOF'\n{MUTABILITY_HELP}EOF\n"))
    readsb = str(script(decoder_env, "readsb", f"cat <<'EOF'\n{READSB_HELP}EOF\n"))
    common = ["--quiet", "--write-json", "/run/x", "--write-json-every", "1"]
    assert adsb_decoder.build_command(fa, "/run/x") == [fa, "--device-type", "rtlsdr", *common]
    assert adsb_decoder.build_command(old, "/run/x", ppm=-2, bias_tee=True, position=(43.04, -87.91)) == \
        [old, *common, "--ppm", "-2", "--lat", "43.0400", "--lon", "-87.9100"]
    assert adsb_decoder.build_command(readsb, "/run/x", ppm=0, bias_tee=True) == \
        [readsb, "--device-type", "rtlsdr", *common, "--enable-biastee"]
    assert "--enable-biastee" not in adsb_decoder.build_command(readsb, "/run/x", bias_tee=False)


def test_decoders_are_looked_for_in_order_and_checked(decoder_env):
    assert adsb_decoder.find_program() is None
    assert "No ADS-B decoder" in adsb_decoder.problem_with(None)
    ancient = script(decoder_env, "dump1090", 'echo "--interactive --net"\n')
    assert adsb_decoder.find_program() == str(ancient)
    assert "too old" in adsb_decoder.problem_with(str(ancient))
    broken = script(decoder_env, "readsb", "exit 127\n")
    assert adsb_decoder.find_program() == str(broken)
    assert "does not run" in adsb_decoder.problem_with(str(broken))
    good = script(decoder_env, "dump1090-fa", f"cat <<'EOF'\n{FA_HELP}EOF\n")
    assert adsb_decoder.find_program() == str(good) and adsb_decoder.problem_with(str(good)) is None


def test_decoder_runs_is_read_and_cleans_up(decoder_env, tmp_path):
    args = fake_decoder(decoder_env)
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.read() == ([], 0, "ADS-B decoder is not running")
    assert decoder.start(ppm=3, position=HOME) is None
    assert decoder.alive and decoder.program == "dump1090-fa"
    scratch = decoder._dir
    assert scratch.parent == tmp_path, "scratch folder belongs on the RAM disk when there is one"
    assert args.read_text().split() == ["--device-type", "rtlsdr", "--quiet", "--write-json", str(scratch),
                                        "--write-json-every", "1", "--ppm", "3",
                                        "--lat", "43.0400", "--lon", "-87.9100"]
    aircraft, messages, error = decoder.read()
    assert [ac.name for ac in aircraft] == ["UAL123", "KLM1023", "ABCDEF", "~C0FFEE"]
    assert (messages, error) == (4521, "")
    (scratch / "aircraft.json").write_text('{"now": 1, "messa')          # caught half-written
    assert decoder.read() == (aircraft, 4521, ""), "a bad read must keep the last good list"
    process = decoder._proc
    decoder.stop()
    assert process.poll() is not None and not scratch.exists() and not decoder.alive
    decoder.stop()                                                       # twice is harmless


def test_decoder_that_cannot_get_the_dongle_says_why(decoder_env, tmp_path):
    fake_decoder(decoder_env, data=None, then='echo "rtlsdr: no supported devices found." >&2\nexit 1')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start() == "dump1090-fa stopped: rtlsdr: no supported devices found."
    assert not decoder.alive and not list(tmp_path.glob("pisdr-adsb-*"))
    fake_decoder(decoder_env, data=None, then="exit 3")                  # says nothing at all
    assert decoder.start() == "dump1090-fa stopped: exit code 3"


def test_decoder_that_dies_later_is_noticed(decoder_env):
    fake_decoder(decoder_env, then='sleep 0.5\necho "rtlsdr: device disconnected" >&2\n'
                                   'echo "Abnormal exit." >&2\nexit 1')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start() is None
    assert decoder.read()[2] == ""
    decoder._proc.wait(timeout=5)
    aircraft, messages, error = decoder.read()
    assert aircraft == [] and messages == 4521
    assert error == "dump1090-fa stopped: rtlsdr: device disconnected"
    decoder.stop()


def test_no_decoder_installed(decoder_env):
    assert "No ADS-B decoder (dump1090) found" in adsb_decoder.AdsbDecoder().start()


def test_bias_tee_goes_through_rtl_biast_when_the_decoder_has_no_switch(decoder_env, tmp_path):
    fake_decoder(decoder_env, help_text=MUTABILITY_HELP, name="dump1090-mutability")
    log = tmp_path / "biast.args"
    script(decoder_env, "rtl_biast", f'echo "$@" > {log}\n')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start(bias_tee=False) is None and not log.exists()
    assert decoder.start(bias_tee=True) is None                          # (restarts the decoder)
    assert log.read_text().split() == ["-b", "1"]
    decoder.stop()
    (decoder_env / "rtl_biast").unlink()
    assert decoder.start(bias_tee=True) is None, "no rtl_biast: carry on without the bias tee"
    decoder.stop()


# --------------------------------------------------------------------------- back ends
def test_simulator_lends_the_dongle_and_takes_it_back(sim, state, qapp, tmp_path):   # noqa: F811
    state.set_recording(True)
    assert sim.start_adsb_mode() is None
    assert state.adsb_mode and state.dongle_lent and not state.recording and not sim.running
    assert state.aircraft == [] and sim.start_adsb_mode() is None
    sim.poll_adsb()
    assert len(state.aircraft) >= 10 and state.adsb_messages > 0 and state.adsb_error == ""
    near = [ac for ac in state.aircraft if ac.has_position]
    adsb.locate(near, *config.ADSB_SIM_HOME)
    assert max(ac.distance_nm for ac in near) < 400, "traffic belongs around the simulator's home"
    # Nothing else may use the receiver meanwhile.
    state.set_recording(True)
    assert not state.recording
    assert sim.play_file(write_wav(tmp_path / "ok.wav")).startswith("Leave network mode / the ADS-B map")
    assert sim.start_network_mode() == "Leave the ADS-B map first" and not state.network_mode
    sim.frames.clear()
    pump(qapp, 0.25)
    assert sim.frames == [], "the spectrum kept running without a dongle"
    assert sim.stop_adsb_mode() is None
    assert not state.adsb_mode and state.aircraft == [] and sim.running
    pump(qapp, 0.3)
    assert sim.frames, "spectrum did not come back"
    sim.poll_adsb()                                                      # a late timer tick is ignored
    assert state.aircraft == []


def test_simulated_traffic_flies_around_the_set_location(sim, state):   # noqa: F811
    state.wx_lat, state.wx_lon = 51.5, -0.1
    sim.start_adsb_mode()
    sim.poll_adsb()
    placed = [ac for ac in state.aircraft if ac.has_position]
    adsb.locate(placed, 51.5, -0.1)
    assert min(ac.distance_nm for ac in placed) < 60
    sim.stop()
    assert not sim._adsb_timer.isActive()


def test_network_mode_keeps_the_map_out(sim, state):   # noqa: F811
    assert sim.start_network_mode() is None
    assert sim.start_adsb_mode() == "Leave network mode first" and not state.adsb_mode
    assert sim.stop_network_mode() is None


def test_record_timer_waits_while_the_map_has_the_dongle(sim, state, timer, clock):   # noqa: F811
    arm(state, 20 * 60, length_min=30)
    sim.start_adsb_mode()
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    assert not state.recording and state.adsb_mode
    sim.stop_adsb_mode()
    clock.wall = utc(20, 5)
    timer.check(clock.wall)                            # the rest of the window is still recorded
    assert state.recording and state.vfo_hz == 6_840_000
    state.set_recording(False)


@needs_fake_gr
def test_adsb_mode_hands_the_dongle_over_and_takes_it_back(radio, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.set_ppm(-3)
    state.set_dsp_nr_level(3)
    state.wx_lat, state.wx_lon = HOME
    state.set_recording(True)
    assert radio.start_adsb_mode() is None
    assert state.adsb_mode and not state.recording
    assert open_sources() == 0, "dongle still open: the decoder could not have claimed it"
    assert radio.adsb.alive and radio._adsb_timer.isActive()
    argv = args.read_text().split()
    assert argv[argv.index("--ppm") + 1] == "-3" and argv[argv.index("--lat") + 1] == "43.0400"
    radio.poll_adsb()
    assert [ac.name for ac in state.aircraft][:2] == ["UAL123", "KLM1023"] and state.adsb_messages == 4521
    state.set_vfo(7_000_000)                    # ignored while the dongle is away; must not raise
    state.set_volume(20)
    assert radio.start_network_mode() == "Leave the ADS-B map first"

    process, scratch = radio.adsb._proc, radio.adsb._dir
    assert radio.stop_adsb_mode() is None
    assert not state.adsb_mode and state.aircraft == [] and not radio._adsb_timer.isActive()
    assert process.poll() is not None and not scratch.exists()
    assert open_sources() == 1 and radio.tb.running
    assert radio.volume.last("set_k") == (0.2 ** 2,)
    assert radio.src.last("set_freq_corr") == (-3, 0)
    assert radio.src.last("set_center_freq") is not None


@needs_fake_gr
def test_an_ip_guess_of_the_location_is_not_given_to_the_decoder(radio, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.space_wx = types.SimpleNamespace(wx={"lat": 43.0, "lon": -88.0})
    assert radio.start_adsb_mode() is None
    assert "--lat" not in args.read_text().split()
    radio.stop_adsb_mode()


@needs_fake_gr
def test_adsb_mode_falls_back_when_the_decoder_cannot_start(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env, data=None, then='echo "usb_claim_interface error -6" >&2\nexit 1')
    assert radio.start_adsb_mode() == "dump1090-fa stopped: usb_claim_interface error -6"
    assert not state.adsb_mode and not radio.radio_down and not radio._adsb_timer.isActive()
    assert open_sources() == 1 and radio.tb.running
    state.set_vfo(7_000_000)
    assert radio.src.last("set_center_freq") is not None


@needs_fake_gr
def test_adsb_mode_without_a_decoder_leaves_the_radio_alone(radio, state, decoder_env):   # noqa: F811
    src = radio.src
    assert "No ADS-B decoder" in radio.start_adsb_mode()
    assert radio.src is src and radio.tb.running and not state.adsb_mode


@needs_fake_gr
def test_decoder_dying_is_reported_and_back_still_returns_the_radio(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env, then='sleep 0.5\necho "rtlsdr: device disconnected" >&2\nexit 1')
    assert radio.start_adsb_mode() is None
    radio.adsb._proc.wait(timeout=5)
    radio.poll_adsb()
    assert state.adsb_mode and state.adsb_error == "dump1090-fa stopped: rtlsdr: device disconnected"
    assert radio.stop_adsb_mode() is None
    assert open_sources() == 1 and radio.tb.running and state.adsb_error == ""


@needs_fake_gr
def test_quitting_from_the_map_stops_the_decoder(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env)
    assert radio.start_adsb_mode() is None
    process = radio.adsb._proc
    radio.stop()
    assert process.poll() is not None and not radio._adsb_timer.isActive()


# --------------------------------------------------------------------------- the page
def adsb_tile(window) -> int:
    return next(i for i, band in enumerate(window.state.bands) if adsb.is_adsb_band(band))


@pytest.fixture
def chart(window, qapp):
    """The map open on the simulator with the receiver's location set, one poll in."""
    window.state.wx_lat, window.state.wx_lon = HOME
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    return window.adsb_page


def test_picking_the_adsb_band_opens_the_map(window, qapp, chart):
    state = window.state
    assert state.adsb_mode and window.stack.currentIndex() == window.PAGE_ADSB
    assert state.band.name == "ADS-B 1090" and state.vfo_hz == config.ADSB_FREQ_HZ
    assert [b.text() for b in window.softkeys.buttons] == ["ZOOM \u2212", "ZOOM +", "LABELS FULL", "BACK"]
    assert "ADS-B MAP" in window.topbar_left.text() and "KNOB SELECT" in window.topbar_right.text()
    colours, brightness = image_stats(window)
    assert colours > 40 and brightness > 3, "map is blank"
    radar = chart.radar
    assert radar.centre == HOME and radar.centre_source == "settings"
    assert len(radar._hits) >= 8, "aircraft were not drawn"
    inside = [1 for _icao, x, y in radar._hits if 0 <= x <= radar.width() and 0 <= y <= radar.height()]
    assert len(inside) >= 6
    assert "aircraft" in chart.stats.text() and "on map" in chart.stats.text()
    window.key_pressed(3)                              # BACK
    assert not state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert window.backend.running and state.vfo_hz == config.ADSB_FREQ_HZ
    assert "ADS-B MAP" not in window.topbar_left.text()


def test_settings_row_opens_the_map_without_retuning(window, qapp):
    state = window.state
    state.set_vfo(14_074_000)
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.selected = next(i for i, r in rows(window) if r.label.startswith("ADS-B"))
    window.key_pressed(2)
    assert state.adsb_mode and window.stack.currentIndex() == window.PAGE_ADSB
    window.key_pressed(3)
    assert not state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert state.vfo_hz == 14_074_000


def test_map_locks_the_ui_until_back(window, qapp, chart):
    for action in (window.show_main, lambda: window.key_long_pressed(0), lambda: window.key_long_pressed(2),
                   lambda: window.key_long_pressed(3), window.encoder_held, window.encoder_pushed,
                   lambda: window.knob_turned(5), lambda: press(window, QtCore.Qt.Key.Key_Escape),
                   lambda: press(window, QtCore.Qt.Key.Key_5), lambda: window.show_page(window.PAGE_SETTINGS),
                   window._enter_network, window._enter_adsb):
        vfo = window.state.vfo_hz
        action()
        assert window.stack.currentIndex() == window.PAGE_ADSB and window.state.adsb_mode
        assert window.state.vfo_hz == vfo and not window.state.network_mode
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN


def test_knob_walks_the_aircraft_nearest_first(window, qapp, chart):
    order = chart.order
    distances = [ac.distance_nm for ac in order if ac.distance_nm is not None]
    assert distances == sorted(distances) and len(distances) >= 8
    assert order[-1].distance_nm is None, "aircraft without a position go last"
    assert chart.selected == "" and "Turn the knob" in chart.detail.text()
    window.knob_turned(1)
    assert chart.selected == order[0].icao and chart.rows[0].property("selected")
    assert order[0].name in chart.detail.text() and "nm" in chart.detail.text()
    window.knob_turned(3)                              # one aircraft per movement, however fast
    assert chart.selected == order[1].icao
    for _ in range(len(order) - 2):
        window.knob_turned(1)
    assert chart.selected == order[-1].icao
    assert "no position received yet" in chart.detail.text()
    assert any(row.property("selected") for row in chart.rows), "the list did not scroll to the selection"
    assert chart.more.text().endswith(f"of {len(order)}")
    window.knob_turned(1)
    assert chart.selected == order[0].icao             # wraps
    window.encoder_pushed()
    assert chart.selected == "" and not any(row.property("selected") for row in chart.rows)
    window.knob_turned(-1)
    assert chart.selected == order[-1].icao


def test_tapping_selects_on_the_map_and_in_the_list(window, qapp, chart):
    radar = chart.radar
    icao, x, y = min(radar._hits, key=lambda hit: math.hypot(hit[1] - radar.width() / 2, hit[2] - 90))
    tap(radar, QtCore.QPoint(int(x) + 4, int(y) - 3))
    assert chart.selected == icao == radar.selected
    window.backend.poll_adsb()                         # the selection follows the aircraft as it moves
    pump(qapp, 0.05)
    assert chart.selected == icao
    empty = next(QtCore.QPoint(px, py) for px in range(10, radar.width(), 15) for py in range(10, radar.height(), 15)
                 if all(math.hypot(px - hx, py - hy) > adsb_page.TAP_RADIUS_PX + 2 for _i, hx, hy in radar._hits))
    tap(radar, empty)
    assert chart.selected == ""
    tap(chart.rows[2], QtCore.QPoint(20, 10))
    assert chart.selected == chart.order[2].icao


def test_zoom_and_label_keys(window, qapp, chart):
    state = window.state
    ranges = [nm for nm, _ring in config.ADSB_RANGES_NM]
    radar = chart.radar
    scale = radar.scale()
    window.key_pressed(1)                              # ZOOM +
    assert state.adsb_range_nm == ranges[ranges.index(config.ADSB_DEFAULT_RANGE_NM) - 1]
    assert radar.scale() > scale
    for _ in range(len(ranges) + 2):
        window.key_pressed(0)                          # ZOOM - stops at the widest view
    assert state.adsb_range_nm == ranges[-1]
    pump(qapp, 0.05)
    east, north = adsb.project(*HOME, HOME[0] + ranges[-1] / 60.0, HOME[1])
    assert radar.to_screen(float(east), float(north))[1] == pytest.approx(adsb_page.EDGE_PX, abs=1.5), \
        "the selected range should reach the top of the plot"
    for expected in ("NAME", "OFF", "FULL"):
        window.key_pressed(2)
        assert state.adsb_labels == expected
        assert window.softkeys.buttons[2].text() == f"LABELS {expected}"
        pump(qapp, 0.02)
    state.save()
    saved = json.loads(config.SETTINGS_FILE.read_text())
    assert (saved["adsb_range_nm"], saved["adsb_labels"]) == (ranges[-1], "FULL")


def test_labels_follow_the_setting():
    cruise = Aircraft("A1B2C3", callsign="UAL123", altitude_ft=35000, vert_rate_fpm=-64)
    climb = Aircraft("ABCDEF", altitude_ft=2449, vert_rate_fpm=1200)
    assert adsb_page.tag_lines(cruise, "FULL", False) == ["UAL123", "350"]
    assert adsb_page.tag_lines(climb, "FULL", False) == ["ABCDEF", "024\u2191"]
    assert adsb_page.tag_lines(cruise, "NAME", False) == ["UAL123"]
    assert adsb_page.tag_lines(cruise, "OFF", False) == [] and adsb_page.tag_lines(cruise, "OFF", True) != []
    assert adsb_page.level_text(Aircraft("X", on_ground=True)) == "GND"
    assert adsb_page.level_text(Aircraft("X", altitude_ft=-200, vert_rate_fpm=-900)) == "000\u2193"
    assert adsb_page.level_text(Aircraft("X")) == ""
    html = adsb_page.detail_html(Aircraft("A1B2C3", callsign="UAL123", lat=43.2, lon=-88.05, altitude_ft=35000,
                                          vert_rate_fpm=-1216, speed_kt=451.6, track_deg=7.0, squawk="1200",
                                          rssi_db=-7.2, distance_nm=11.4, bearing_deg=327.0))
    for piece in ("UAL123", "A1B2C3", "35,000 ft", "\u21931,216 fpm", "452 kt", "007\u00b0", "11.4 nm", "NW", "1200"):
        assert piece in html, piece
    bare = adsb_page.detail_html(Aircraft("ABCDEF"))
    assert "no callsign yet" in bare and "no position received yet" in bare


def test_trails_grow_stay_bounded_and_break_on_a_jump(window, qapp, chart):
    traffic = window.backend._traffic
    icao = chart.order[1].icao
    for _ in range(config.ADSB_TRAIL_POINTS + 20):
        traffic._t0 -= 1.0                             # one second of flight per poll
        window.backend.poll_adsb()
    trail = chart.radar.trails[icao]
    assert len(trail) == config.ADSB_TRAIL_POINTS
    assert set(chart.radar.trails) == {ac.icao for ac in window.state.aircraft if ac.has_position}
    pump(qapp, 0.05)
    traffic._t0 -= 1500.0                              # a long silence: everyone is somewhere else
    window.backend.poll_adsb()
    assert len(chart.radar.trails[icao]) == 1
    window.state.set_aircraft([])
    assert chart.radar.trails == {} and chart.order == []
    pump(qapp, 0.05)
    assert chart.radar._overlay()[0] == "Listening on 1090 MHz..."


def test_map_without_a_location_centres_on_the_traffic(window, qapp):
    window._enter_adsb()
    page = window.adsb_page
    assert page.radar.centre is None and page.radar._overlay()[0].startswith("Listening")
    pump(qapp, 0.05)                                   # draws with nothing to centre on
    window.state.set_aircraft([Aircraft("ABCDEF", callsign="N1")], 10)
    assert "waiting for its position" in page.radar._overlay()[0]
    window.backend.poll_adsb()
    centre = page.radar.centre
    assert page.radar.centre_source == "traffic" and centre is not None
    assert adsb.distance_bearing(*centre, *config.ADSB_SIM_HOME)[0] < 150
    assert all(ac.distance_nm is None for ac in page.order), "no receiver position: no distances to show"
    assert all("nm" not in row.right.text() for row in page.rows)
    window.knob_turned(1)
    assert "\u00b0N" in page.detail.text()
    window.backend._traffic._t0 -= 300.0
    window.backend.poll_adsb()
    assert page.radar.centre == centre, "the map must not slide about with the traffic"
    pump(qapp, 0.05)
    assert len(page.radar._hits) >= 5
    window.key_pressed(3)


def test_decoder_failure_is_shown_on_the_map(window, qapp, chart, monkeypatch):
    monkeypatch.setattr(window.backend, "_hw_adsb_poll",
                        lambda: ([], 120, "dump1090-fa stopped: rtlsdr: device disconnected"))
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    text = chart.radar._overlay()[0]
    assert "device disconnected" in text and "Press BACK" in text
    assert window.stack.currentIndex() == window.PAGE_ADSB and window.state.adsb_mode
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN and window.state.adsb_error == ""


def test_no_decoder_means_a_message_and_an_ordinary_band_change(window, qapp, monkeypatch):
    monkeypatch.setattr(window.backend, "_hw_adsb", lambda on: "No ADS-B decoder (dump1090) found - run install.sh")
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    assert window.stack.currentIndex() == window.PAGE_MAIN and not window.state.adsb_mode
    assert window.state.band.name == "ADS-B 1090" and window.backend.running
    assert window.toast.text().startswith("No ADS-B decoder")


@pytest.mark.parametrize("theme", config.THEME_NAMES)
def test_map_draws_in_every_theme(window, qapp, theme):
    window.state.set_theme(theme)
    window.state.wx_lat, window.state.wx_lon = HOME
    window._enter_adsb()
    window.backend.poll_adsb()
    window.knob_turned(1)
    pump(qapp, 0.05)
    colours, _ = image_stats(window)
    assert colours > 40
    window.key_pressed(3)


# --------------------------------------------------------------------------- real decoder
def _real_decoders() -> list[str]:
    named = [path for path in os.environ.get("PISDR_TEST_DUMP1090", "").split(os.pathsep) if path]
    found = adsb_decoder.find_program()
    return named or ([found] if found else [])


@pytest.mark.parametrize("exe", _real_decoders() or [None], ids=lambda exe: Path(exe).name if exe else "none")
def test_real_dump1090_decodes_a_synthetic_recording(exe, tmp_path, monkeypatch):
    """End to end: Mode S frames -> I/Q file -> a real dump1090 -> aircraft.json -> Aircraft."""
    if exe is None:
        pytest.skip("no dump1090 installed (set PISDR_TEST_DUMP1090 to the path of one)")
    frames = (adsb_signal.frames_for(0xA1B2C3, "UAL123", 43.20, -88.05, 35000, 452, 273, -1216)
              + adsb_signal.frames_for(0x40621D, "KLM1023", 43.85, -87.10, 12025, 250, 45, 640)
              + [adsb_signal.identification(0xABCDEF, "N123PI")])
    recording = tmp_path / "adsb.iq"
    adsb_signal.write_iq(recording, frames, seconds=3.0)
    # A wrapper with the decoder's own name, so the app's command line is used unchanged and
    # the recording is appended as the signal source instead of the dongle.
    name = Path(exe).name
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script(bin_dir, name, f'exec {exe} "$@" --ifile {recording} --iformat UC8 --throttle\n')
    monkeypatch.setattr(config, "ADSB_PROGRAMS", (name,))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(adsb_decoder, "_HELP", {})

    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start(ppm=1, position=HOME) is None
    try:
        found: dict[str, Aircraft] = {}
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            aircraft, messages, error = decoder.read()
            found.update({ac.icao: ac for ac in aircraft})
            complete = [ac for ac in found.values() if ac.has_position and ac.speed_kt and ac.callsign]
            if error or (len(complete) == 2 and len(found) == 3):
                break
            time.sleep(0.2)
    finally:
        decoder.stop()
    assert set(found) == {"A1B2C3", "40621D", "ABCDEF"}, f"decoded {sorted(found)} ({error or 'no error'})"
    assert messages > 100
    united, klm, quiet = found["A1B2C3"], found["40621D"], found["ABCDEF"]
    assert (united.callsign, klm.callsign, quiet.callsign) == ("UAL123", "KLM1023", "N123PI")
    assert (united.lat, united.lon) == pytest.approx((43.20, -88.05), abs=0.001)
    assert (klm.lat, klm.lon) == pytest.approx((43.85, -87.10), abs=0.001)
    assert (united.altitude_ft, klm.altitude_ft) == (35000, 12025)
    assert united.speed_kt == pytest.approx(452, abs=1.5) and united.track_deg == pytest.approx(273, abs=1.0)
    assert klm.speed_kt == pytest.approx(250, abs=1.5) and klm.track_deg == pytest.approx(45, abs=1.0)
    assert (united.vert_rate_fpm, klm.vert_rate_fpm) == (-1216, 640)
    assert not quiet.has_position and quiet.altitude_ft is None
    adsb.locate([united, klm], *HOME)
    assert united.distance_nm == pytest.approx(11.4, abs=0.1) and klm.distance_nm == pytest.approx(60.1, abs=0.1)
    assert adsb.compass_point(united.bearing_deg) == "NW" and adsb.compass_point(klm.bearing_deg) == "NE"
