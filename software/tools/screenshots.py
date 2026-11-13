#!/usr/bin/env python3
"""Render the screens used in the documentation, from the simulator, with no display.

    cd software
    python tools/screenshots.py              # writes ../docs/images/ui_*.png
    python tools/screenshots.py /tmp/shots   # or somewhere else

Runs offscreen with a throw-away settings folder, so it never touches your own settings,
memories or recordings. The spectrum and the aircraft are the simulator's and the HF
conditions numbers are sample values, not live data. Also a quick way to look at a UI
change at the real 800x480.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_scratch = tempfile.mkdtemp(prefix="pisdr-shots-")
os.environ["PISDR_CONFIG_DIR"] = os.path.join(_scratch, "config")
os.environ["PISDR_RECORDINGS_DIR"] = os.path.join(_scratch, "recordings")

SOFTWARE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOFTWARE_DIR))

from pisdr import config, spacewx  # noqa: E402
from pisdr.qt import QtWidgets  # noqa: E402
from pisdr.radio.backend_sim import SimBackend  # noqa: E402
from pisdr.state import RadioState  # noqa: E402
from pisdr.ui.main_window import MainWindow  # noqa: E402

SAMPLE_WX = spacewx.SpaceWx(
    sfi=168, ssn=133, a_index=7, k_index=2, xray="C1.4", solar_wind=402.7, bz=-1.2,
    geomag="Very quiet", noise="S1-S2", aurora=2, muf="21.3",
    bands={"80m-40m": ["Fair", "Good"], "30m-20m": ["Good", "Good"],
           "17m-15m": ["Good", "Fair"], "12m-10m": ["Fair", "Poor"]},
    kp_history=[1.0, 1.33, 2.0, 2.67, 3.33, 2.33, 2.0, 1.67],
    wx={"temp": 54.0, "temp_unit": "°F", "humidity": 62.0, "pressure": 1018.0, "wind": 7.0,
        "wind_unit": "mph", "wind_dir": 250.0, "code": 2, "desc": "Partly cloudy", "city": "Sample"},
)


WATERFALL_FILL_S = 9.0      # long enough for the waterfall to scroll its full height
# The test suite sets this to run the script quickly; the images are then not worth keeping.
SETTLE_OVERRIDE = os.environ.get("PISDR_SHOT_SETTLE")


class Studio:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        spacewx.fetch_text = _no_network                       # sample data only
        self.state = RadioState()
        self.backend = SimBackend(self.state, reason="--sim requested")
        self.window = MainWindow(self.state, self.backend, windowed=True)
        self.window.spacewx.stop()
        self.backend.start()
        self.window.show()
        self.set_wx()

    def set_wx(self) -> None:
        now = time.time()
        SAMPLE_WX.solar_at = SAMPLE_WX.kp_at = SAMPLE_WX.wx_at = now
        self.state.set_space_wx(SAMPLE_WX)

    def band(self, name: str) -> None:
        self.state.select_band([band.name for band in self.state.bands].index(name))

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def shot(self, name: str, settle_s: float = 0.4) -> None:
        self.pump(float(SETTLE_OVERRIDE) if SETTLE_OVERRIDE else settle_s)
        self.window._toast_timer.stop()
        self.window.toast.hide()
        path = self.out_dir / f"{name}.png"
        if not self.window.grab().save(str(path)):
            raise SystemExit(f"could not write {path}")
        print(f"wrote {path}")


def _no_network(url: str) -> str:
    raise OSError("network disabled while taking screenshots")


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else SOFTWARE_DIR.parent / "docs" / "images"
    studio = Studio(out_dir)
    s, w = studio.state, studio.window

    # 2 m band with the simulator's stations, signal markers and tags.
    s.set_hf_panel("OFF")
    studio.band("2 m")
    studio.shot("ui_main_sim", settle_s=WATERFALL_FILL_S)

    s.cycle_spectrum_zoom(config.SPECTRUM_ZOOMS.index(8))
    studio.shot("ui_zoom", settle_s=WATERFALL_FILL_S)
    s.step_spectrum_zoom(-10)

    # 40 m with the HF conditions panel.
    s.set_hf_panel("AUTO")
    studio.band("40 m")
    studio.shot("ui_hf_conditions", settle_s=WATERFALL_FILL_S)

    for theme in ("Inferno", "Green"):
        s.set_theme(theme)
        studio.shot(f"ui_theme_{theme.lower()}", settle_s=1.0)
    s.set_theme(config.DEFAULT_THEME)

    w.show_page(w.PAGE_MENU)
    studio.shot("ui_menu")
    w.show_page(w.PAGE_BAND)
    studio.shot("ui_band_picker")
    w.show_page(w.PAGE_SETTINGS)
    w.settings_page._select_tab(w.settings_page.TABS.index("DISPLAY"))
    studio.shot("ui_settings")
    s.import_preset_memories()
    w.show_page(w.PAGE_MEMORIES)
    studio.shot("ui_memories")

    # ADS-B map: the simulator's traffic around a sample location, flown for a while first
    # so the aircraft have trails, with one of them selected.
    s.wx_lat, s.wx_lon = config.ADSB_SIM_HOME
    w.show_main()
    w._enter_adsb()
    for _ in range(config.ADSB_TRAIL_POINTS):
        studio.backend._traffic._t0 -= 1.0             # one second of flight per poll
        studio.backend.poll_adsb()
    w.adsb_page._counted = None                        # (the fast-forward is not a message rate)
    for _ in range(4):
        w.knob_turned(1)
    studio.shot("ui_adsb_map", settle_s=2.3)           # two real polls: a true msg/s figure
    w.key_pressed(3)

    w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
