"""Which RTL-SDR dongles are attached, what each can receive, and which one tunes a frequency.

The radio has two dongles with different tuner chips:

* **Nooelec NESDR SMArt XTR** (Elonics E4000): the main tuner. About 65-2300 MHz with a gap
  around 1100-1250 MHz, no HF, no bias tee.
* **RTL-SDR Blog V3** (Rafael Micro R820T2): decodes ADS-B on 1090 MHz. It also tunes
  24-1766 MHz and, by direct sampling on the Q branch, 500 kHz-24 MHz, so it receives what
  the E4000 cannot. It has the bias tee.

The dongles are told apart by their tuner chip, which librtlsdr reports when it opens a
device. That needs no set-up and works when both carry the factory serial number.
``config.MAIN_TUNER`` / ``config.ADSB_TUNER`` override it with a serial number or an index.

One dongle on its own (either of these, or a Blog V4) still works: it is then both the
receiver and the ADS-B decoder's dongle, and is lent to the decoder while the map is open.

Nothing here needs GNU Radio; only :func:`scan` touches hardware.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import shutil
import subprocess
from dataclasses import dataclass

from .. import config

log = logging.getLogger(__name__)

# enum rtlsdr_tuner in rtl-sdr.h
_E4000, _R820T, _R828D = 1, 5, 6


@dataclass(frozen=True)
class TunerSpec:
    """What one kind of dongle can do."""

    key: str                                  # short name for the screen: XTR, V3 ...
    name: str
    ranges: tuple[tuple[int, int], ...]       # what the tuner chip covers, Hz
    direct: tuple[int, int] | None = None     # what direct sampling adds below that
    gain_max_db: float = config.GAIN_MAX_DB
    bias_tee: bool = True

    def direct_at(self, hz: float) -> bool:
        """True where *hz* is received by direct sampling rather than through the tuner chip."""
        return self.direct is not None and hz < self.direct[1]

    def distance(self, hz: float) -> float:
        """How far *hz* is outside what this dongle receives (0 = it is covered)."""
        spans = list(self.ranges) + ([self.direct] if self.direct else [])
        return min(max(lo - hz, hz - hi, 0.0) for lo, hi in spans)

    def covers(self, hz: float) -> bool:
        return self.distance(hz) == 0.0


XTR = TunerSpec("XTR", "NESDR SMArt XTR (E4000)", config.XTR_RANGES_HZ,
                gain_max_db=config.XTR_GAIN_MAX_DB, bias_tee=False)
V3 = TunerSpec("V3", "RTL-SDR Blog V3 (R820T2)", (config.V3_RANGE_HZ,), direct=config.V3_DIRECT_RANGE_HZ)
V4 = TunerSpec("V4", "RTL-SDR Blog V4 (R828D)", (config.V4_RANGE_HZ,))
# A dongle that could not be identified: nothing is refused and nothing special is done.
GENERIC = TunerSpec("RTL", "RTL-SDR", ((config.FREQ_MIN, config.FREQ_MAX),))

_BY_CHIP = {_E4000: XTR, _R820T: V3, _R828D: V4}


@dataclass(frozen=True)
class Dongle:
    """One attached RTL-SDR. *index* is librtlsdr's device index, which every tool takes."""

    index: int
    spec: TunerSpec = GENERIC
    manufacturer: str = ""
    product: str = ""
    serial: str = ""

    def describe(self) -> str:
        usb = " ".join(part for part in (self.manufacturer, self.product) if part)
        return f"#{self.index} {self.spec.name}" + (f" [{usb}, SN {self.serial}]" if usb else "")


@dataclass(frozen=True)
class Source:
    """A dongle and the way it is opened: what the receiver is listening through."""

    dongle: Dongle
    direct: bool = False

    @property
    def label(self) -> str:
        return f"{self.dongle.spec.key} HF" if self.direct else self.dongle.spec.key

    def covers(self, hz: float) -> bool:
        return self.dongle.spec.covers(hz)

    def device_args(self, bias_tee: bool = False) -> str:
        """The gr-osmosdr device string. These options only take effect when it is opened."""
        args = f"rtl={self.dongle.index}"
        if self.direct:
            args += ",direct_samp=2"          # 2 = Q branch, where the V3's HF input is
        if bias_tee and self.dongle.spec.bias_tee:
            args += ",bias=1"
        return args


@dataclass(frozen=True)
class Lineup:
    """The dongles by job. *aux* is the second dongle, if there is one."""

    main: Dongle = Dongle(0)
    aux: Dongle | None = None

    @property
    def adsb(self) -> Dongle:
        """The dongle the ADS-B decoder runs on."""
        return self.aux if self.aux is not None else self.main

    @property
    def dongles(self) -> list[Dongle]:
        return [self.main] if self.aux is None else [self.main, self.aux]

    def named(self, prefer: str) -> Dongle | None:
        """The dongle a ``config.TUNER_PREFS`` entry stands for (None for AUTO)."""
        return {"MAIN": self.main, "AUX": self.aux}.get(prefer)

    def route(self, hz: float, prefer: str = "AUTO", aux_free: bool = True) -> Source:
        """What should receive *hz*.

        AUTO takes the main tuner where it covers the frequency and the second dongle where
        only that does. MAIN / AUX keep to one dongle. *aux_free* is False while the ADS-B
        decoder has the second dongle. A frequency nothing covers goes to the nearest.
        """
        if self.aux is None or not aux_free or prefer == "MAIN":
            choices = [self.main]
        elif prefer == "AUX":
            choices = [self.aux]
        else:
            choices = [self.main, self.aux]
        best = min(choices, key=lambda dongle: dongle.spec.distance(hz))     # first of equals: main
        return Source(best, best.spec.direct_at(hz))


# --------------------------------------------------------------------------- who is who
def identify(chip: int, manufacturer: str = "", product: str = "") -> TunerSpec:
    """The kind of dongle, from its tuner chip (0 = could not be read) and USB strings."""
    if chip in _BY_CHIP:
        return _BY_CHIP[chip]
    text = f"{manufacturer} {product}".lower()
    if "xtr" in text:
        return XTR
    if "blog v4" in text:
        return V4
    return GENERIC


def _named(dongles: list[Dongle], wanted: str) -> Dongle | None:
    """The dongle with serial number *wanted*, or at index *wanted*."""
    wanted = str(wanted).strip()
    if not wanted:
        return None
    for dongle in dongles:
        if dongle.serial == wanted:
            return dongle
    for dongle in dongles:
        if wanted.isdigit() and dongle.index == int(wanted):
            return dongle
    log.warning("no RTL-SDR with serial number or index %r; choosing by tuner chip instead", wanted)
    return None


def assign(dongles: list[Dongle]) -> Lineup:
    """Give the attached dongles their jobs."""
    if not dongles:
        return Lineup()                       # opening device 0 will report what is wrong
    if len(dongles) == 1:
        return Lineup(dongles[0])
    main = _named(dongles, config.MAIN_TUNER)
    aux = _named([d for d in dongles if d is not main], config.ADSB_TUNER)
    rest = [d for d in dongles if d is not main and d is not aux]
    if main is None:
        # The XTR; failing that, one that is not the V3, so that the V3 is left for ADS-B and HF.
        main = next((d for d in rest if d.spec is XTR), None) or \
            next((d for d in rest if not d.spec.direct), rest[0])
        rest.remove(main)
    if aux is None and rest:
        aux = next((d for d in rest if d.spec.direct), rest[0])      # the one that also does HF
    return Lineup(main, aux)


# --------------------------------------------------------------------------- hardware
def _library():
    for name in (ctypes.util.find_library("rtlsdr"), "librtlsdr.so.2", "librtlsdr.so.0"):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
        lib.rtlsdr_get_device_usb_strings.argtypes = [ctypes.c_uint32] + [ctypes.c_char_p] * 3
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        lib.rtlsdr_get_tuner_type.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        return lib
    return None


def _chip(lib, index: int) -> int:
    """Open device *index* just long enough to read its tuner chip; 0 if it cannot be opened."""
    dev = ctypes.c_void_p()
    if lib.rtlsdr_open(ctypes.byref(dev), index) != 0 or not dev:
        return 0
    try:
        return int(lib.rtlsdr_get_tuner_type(dev))
    finally:
        lib.rtlsdr_close(dev)


def scan() -> list[Dongle]:
    """Every RTL-SDR on USB. Opens each one briefly, so call it before anything else has them."""
    lib = _library()
    if lib is None:
        log.warning("librtlsdr not found; assuming one RTL-SDR at index 0")
        return []
    found = []
    for index in range(int(lib.rtlsdr_get_device_count())):
        strings = [ctypes.create_string_buffer(256) for _ in range(3)]
        lib.rtlsdr_get_device_usb_strings(index, *strings)
        manufacturer, product, serial = (s.value.decode("utf-8", "replace").strip() for s in strings)
        chip = _chip(lib, index)
        if chip == 0:
            log.warning("RTL-SDR #%d (%s %s) could not be opened to read its tuner chip", index, manufacturer, product)
        found.append(Dongle(index, identify(chip, manufacturer, product), manufacturer, product, serial))
    return found


def detect() -> Lineup:
    """Look at USB and decide which dongle does what."""
    lineup = assign(scan())
    log.info("main tuner: %s", lineup.main.describe())
    if lineup.aux is not None:
        log.info("second tuner (ADS-B, HF): %s", lineup.aux.describe())
    return lineup


def switch_bias_tee(index: int | None, on: bool) -> bool:
    """Set the bias tee of a dongle that nothing has open, with ``rtl_biast``. It stays as set
    until the dongle loses power. *index* None = the tool's default device. False if it could
    not be done."""
    tool = shutil.which("rtl_biast")
    if not tool:
        return False
    cmd = [tool] + (["-d", str(index)] if index is not None else []) + ["-b", "1" if on else "0"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("rtl_biast failed: %s", exc)
        return False
