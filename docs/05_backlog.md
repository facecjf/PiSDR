# 05 - Features to add

The receiver today is one RTL-SDR Blog V4, one `osmosdr.source` at 1.92 MS/s, and one demodulator. `software/config/bands.json` only retunes that same radio.

## Done

Everything here runs on the single V4; user-facing details are in `docs/02_software_setup.md` sections 5–5f.

- **Band scan** (and memory scan): steps the current band, holds above a trigger level, resumes after it goes quiet. Dwell, resume (or stay) and trigger level are settings.
- **Scan lockouts** (long-press SKIP, or from the memories page), a **priority channel** checked every 2–10 s while scanning, and a **scan log** of every hold with a page to re-tune or lock out hits.
- **Memory groups** and UTC **schedules** (`utc`, `days`) with an ON NOW filter on the memories page and in memory scan. The numbers/military presets carry groups and the known schedules.
- **Receiver settings**: filter width per mode, PPM correction, S-meter calibration, CW pitch.
- **Display**: UTC clock, DSI backlight brightness and dim timer, knob acceleration, spectrum zoom ×2/×4/×8, averaging, peak hold.
- **Spectrum**: zoom to ×64 with on-screen buttons, filled gradient trace and waterfall palettes, signal detection markers, seek and snap-to-signal (with filter fit), frequency tags from memories / RDS / a built-in list.
- **Recording**: recordings browser with playback through the radio's audio, and a UTC record timer (once or daily) that tunes, records and tunes back. Recordings are taken before the volume control.
- **Decoders**: RDS (PS / PTY / RT) on FM broadcast via optional `gr-rds`; APRS, POCSAG, FLEX, EAS and DTMF via `multimon-ng`.
- **Network mode**: hands the dongle to `rtl_tcp` on port 1234 and takes it back.
- **Location by ZIP code**: Settings → SYSTEM → Location ▸ takes a 5-digit US ZIP code on a number pad and looks it up in a bundled table (GeoNames, CC BY 4.0), with no network needed. It sets the location for the weather and the centre of the ADS-B map; AUTO returns to the IP address guess.
- **ADS-B aircraft map**: picking the ADS-B 1090 band lends the dongle to `dump1090` and shows a radar-style plot (range rings, coastlines and borders, callsigns, altitudes, trails) with a list of the nearest aircraft. `install.sh` builds the decoder; no service, no network ports.
- **Robustness**: settings, memories, tags and the scan log are saved atomically and damaged files are set aside as `*.corrupt` instead of being overwritten; each bad setting falls back on its own; a bug in a handler shows a message instead of closing the program; SIGTERM / poweroff saves and finalises a recording; recordings stop before the card fills.
- **Main screen in piHPSDR's layout**: VFO bar with a status line (mode and filter, AGC, NR, NB, notch, filters, squelch, decoders, bias tee, REC), VFO A and a second VFO B (A<>B, A>B), S-meter in S-units, MENU and HIDE buttons, frequency scale on top of the panadapter, a sliders area (AF, RF, squelch) with a pop-up slider when it is hidden, and a toolbar of three rows of keys behind an FNC key. A menu page reaches every page and Settings tab.
- **Test suite** (`software/tests/`, about 460 tests, PyQt5 and PyQt6) and a screenshot tool (`software/tools/screenshots.py`). See "Developing on a PC" in the README.
- **Fixes found by the tests**: BAND stuck on "HF utility" (and picking 40 m, 20 m, 31 m or WWV selected "HF utility" instead, so band scan covered 3–18 MHz); releasing a manual scan HOLD did not resume; a refused timer recording left the radio on the timer's frequency; "00:00-24:00" schedules were never on; the CW decoder printed stray `E`s on noise; a changed web feed or damaged cache could stop the HF conditions panel being built.

## 1. Multi-band

Listen on one band while another band is scanned. The V4 has one tuner, so this needs a second receiver.

A second RTL-SDR is the fit for this codebase: another `osmosdr.source` with `rtl=1`, hop-and-measure only, no second audio chain until a hit is sent to the listening radio. Two full demodulators and two waterfalls at once are more than a Pi 4 should run.

Also required:

- A second antenna for the band being scanned. A VHF/UHF whip can be split to both dongles (about 3.5 dB loss). HF plus VHF should stay on two antennas; the V4's HF path is the upconverter inside that dongle.
- A powered USB hub when a USB sound dongle is also plugged in. Two V4s plus audio can exceed the Pi 4's shared USB current budget.
- Room in the enclosure. The current shell is sized for one dongle and one SMA bulkhead.

An SDRplay RSPduo is the one-box alternative (two tuners, one USB device). It would replace the `rtl=0` source path with a SoapySDR source.

## 2. Hardware add-ons

Each needs parts and a small amount of code.

- **I2S amplifier and speaker** (e.g. MAX98357A): a built-in speaker instead of the headphone jack. Device-tree overlay plus an ALSA device name.
- **RTC** (DS3231 or the Pi 5 RTC battery): correct UTC for schedules, the record timer and the scan log without Wi-Fi.
- **Second encoder**: a dedicated volume/squelch knob so the main knob always tunes.
- **UPS HAT / battery**: portable use, with a clean shutdown on low battery.

## 3. Software, small

- **CI**: run `python -m pytest` on every push with a GitHub Actions workflow (Python 3.11, `QT_QPA_PLATFORM=offscreen`, `apt install libegl1`). Not added yet because it cannot be tried from a development machine without pushing.
- **multimon-ng exit detection**: if the decoder process dies mid-session the strip just goes quiet; it should switch the decoder to OFF with a message.
- **Settings rows that only go up**: Spectrum rate, Volume and RF gain step upward and wrap with CHANGE; the knob goes both ways. A long-press for "down" would match the other rows.
- **Signal markers at the span edge**: the noise-floor fit is a straight line, so the roll-off at the edge of the tuner's passband can produce a marker there.
- **Verify on hardware**: everything in "Robustness" and "Fixes" above was checked on the simulator and the GNU Radio stand-in only.
