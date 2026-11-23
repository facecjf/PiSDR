"""Entry point: ``python3 -m pisdr [--sim] [--windowed] [--no-gpio]``."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import config


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=3)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
    except OSError:
        pass


def check_radio() -> int:
    """Step through everything the GNU Radio backend needs and print what fails. No GUI."""
    import subprocess
    import traceback

    ok = True

    def step(title, fn):
        nonlocal ok
        print(f"[{title}] ", end="", flush=True)
        try:
            result = fn()
            print("ok" if result is None else f"ok - {result}")
        except Exception:
            ok = False
            print("FAILED")
            traceback.print_exc()
            print()

    def rtl_test():
        out = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=8)
        text = out.stdout + out.stderr
        if "No supported devices" in text:
            raise RuntimeError("no RTL-SDR found on USB (plugged in? `lsusb` shows 0bda:2838?)")
        if "usb_claim_interface" in text:
            raise RuntimeError("dongle held by the DVB kernel driver - blacklist it and reboot (doc 02 s.2)")
        return "librtlsdr can open a dongle"

    lineup = None

    def find_tuners():
        nonlocal lineup
        from .radio import tuners
        found = tuners.scan()
        lineup = tuners.assign(found)
        if not found:
            raise RuntimeError("librtlsdr lists no RTL-SDR")
        for dongle in found:
            print(f"\n    {dongle.describe()}", end="")
        print("\n    ", end="")
        if lineup.aux is None:
            return (f"one dongle: {lineup.main.spec.name} does everything, and the receiver "
                    "pauses while the ADS-B map is open")
        unknown = [d for d in lineup.dongles if d.spec is tuners.GENERIC]
        if unknown:
            raise RuntimeError(f"cannot tell what dongle #{unknown[0].index} is (its tuner chip could not be "
                               "read - is another program using it?). Set PISDR_MAIN_TUNER / PISDR_ADSB_TUNER "
                               "to serial numbers if this persists (doc 02 s.2)")
        return f"main tuner {lineup.main.spec.key} (#{lineup.main.index}), ADS-B and HF {lineup.aux.spec.key} (#{lineup.aux.index})"

    def import_gr():
        from gnuradio import gr
        return f"gnuradio {gr.version()}"

    def import_osmo():
        import osmosdr
        return f"gr-osmosdr importable from {osmosdr.__file__}"

    def open_source():
        import gc
        import osmosdr
        from .radio import tuners
        opened = []
        for dongle in (lineup or tuners.Lineup()).dongles:
            src = osmosdr.source(args=f"numchan=1 {tuners.Source(dongle).device_args()}")
            src.set_sample_rate(config.SAMPLE_RATE)
            opened.append(f"{dongle.spec.key} rate {src.get_sample_rate():.0f}, "
                          f"gains {src.get_gain_range().to_pp_string()}")
            del src
            gc.collect()                           # closes it: the next step opens it again
        return "; ".join(opened)

    def build_demods():
        from .radio.demod import build_all
        return f"{len(build_all())} demodulators built"

    def open_audio():
        import gc
        from .radio.audio_out import alsa_cards, open_sink
        print(f"(ALSA cards: {alsa_cards() or 'none'}) ", end="", flush=True)
        sink, device = open_sink(config.AUDIO_RATE)
        del sink
        gc.collect()
        return f"opened {device}"

    def build_backend():
        from .qt import QtCore
        from .radio.flowgraph import GnuRadioBackend
        from .state import RadioState
        QtCore.QCoreApplication(sys.argv[:1])
        backend = GnuRadioBackend(RadioState(), lineup=lineup)
        return f"GnuRadioBackend constructed, receiving on the {backend.source.label}"

    def optional_extras():
        from . import sysinfo
        from .backlight import Backlight
        from .radio import adsb_decoder
        bl = Backlight()
        found = {
            "gr-rds (RDS)": sysinfo.have_module("rds"),
            "multimon-ng (decoders)": sysinfo.have_tool("multimon-ng"),
            "rtl_tcp (network mode)": sysinfo.have_tool("rtl_tcp"),
            "dump1090 (ADS-B map)": adsb_decoder.problem_with(adsb_decoder.find_program()) is None,
            "backlight": bl.available and bl.writable,
        }
        for name, have in found.items():
            print(f"\n    {name}: {'yes' if have else 'no'}", end="")
        print()
        missing = [n for n, have in found.items() if not have]
        return "all present" if not missing else "optional, re-run install.sh to add: " + ", ".join(missing)

    print("PiSDR radio check")
    step("rtl_test", rtl_test)
    step("gnuradio", import_gr)
    step("tuners", find_tuners)
    step("gr-osmosdr", import_osmo)
    step("open RTL-SDR", open_source)
    step("demodulators", build_demods)
    step("audio", open_audio)
    step("full backend", build_backend)
    radio_ok = ok
    step("extras", optional_extras)
    ok = radio_ok
    print("\nAll good - the app will use the real radio." if ok else
          "\nFix the first FAILED step above; until then the app falls back to the simulator.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pisdr", description="PiSDR receiver")
    parser.add_argument("--sim", action="store_true", help="use the simulated backend (no hardware)")
    parser.add_argument("--windowed", action="store_true", help="run in a normal 800x480 window")
    parser.add_argument("--no-gpio", action="store_true", help="do not start the GPIO controls")
    parser.add_argument("--check", action="store_true", help="diagnose the radio backend and exit (no GUI)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        logging.basicConfig(level=logging.WARNING)
        return check_radio()
    _setup_logging(args.verbose)
    log = logging.getLogger("pisdr")

    # Qt platform: prefer Wayland on the Pi desktop, fall back to X11/xcb.
    if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"

    from . import crashguard
    from .qt import QT_LIB, QtWidgets
    from .radio.backend import make_backend
    from .state import RadioState
    from .ui.main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("PiSDR")
    log.info("Qt binding: %s", QT_LIB)

    state = RadioState()
    state.load()
    backend = make_backend(state, force_sim=args.sim)
    window = MainWindow(state, backend, windowed=args.windowed)
    # A bug in one handler must not take the radio down (PyQt5 would abort the process),
    # and shutdown / reboot / Ctrl-C must close the recording and save settings.
    crashguard.install_excepthook(window.show_message, window.close)
    wake_timer = crashguard.install_signal_handlers(app, window.close)  # noqa: F841 (kept alive)

    controls = None
    if not args.no_gpio:
        try:
            from .controls.gpio import GpioControls
            controls = GpioControls()
            controls.rotated.connect(window.knob_turned)
            controls.encoder_pushed.connect(window.encoder_pushed)
            controls.encoder_held.connect(window.encoder_held)
            controls.key_pressed.connect(window.key_pressed)
            controls.key_long_pressed.connect(window.key_long_pressed)
            log.info("GPIO controls active")
        except Exception:  # gpiozero missing, pins busy, or not on a Pi
            log.exception("GPIO controls not available - the radio works by touch only")
            window.show_message("GPIO controls failed - see ~/.config/pisdr/pisdr.log")

    backend.start()
    if args.windowed:
        window.show()
    else:
        window.showFullScreen()
        target = os.environ.get("PISDR_SCREEN")   # e.g. DSI-1 when an HDMI monitor is also attached
        handle = window.windowHandle()
        if target and handle is not None:
            for screen in app.screens():
                if screen.name() == target:
                    handle.setScreen(screen)
                    window.showFullScreen()

    rc = app.exec()
    if controls is not None:
        controls.close()
    return rc
