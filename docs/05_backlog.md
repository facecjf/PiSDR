# 05 - Features to add

The receiver today is two dongles, an RTL-SDR Blog V3 (main tuner, HF included) and a Nooelec NESDR SMArt XTR (ADS-B, and reception above 1766 MHz), with one `osmosdr.source` at 1.92 MS/s on whichever of them tunes the frequency, and one demodulator. `software/config/bands.json` only retunes that one receiver; the second dongle is used by it in turn, or by `dump1090`, never by a second receiver.

## Done

User-facing details are in `docs/02_software_setup.md` sections 5–5g.

- **Band scan** (and memory scan): steps the current band, holds above a trigger level, resumes after it goes quiet. Dwell, resume (or stay) and trigger level are settings.
- **Scan lockouts** (long-press SKIP, or from the memories page), a **priority channel** checked every 2–10 s while scanning, and a **scan log** of every hold with a page to re-tune or lock out hits.
- **Memory groups** and UTC **schedules** (`utc`, `days`) with an ON NOW filter on the memories page and in memory scan. The numbers/military presets carry groups and the known schedules.
- **Receiver settings**: filter width per mode, PPM correction, S-meter calibration, CW pitch.
- **Display**: UTC clock, DSI backlight brightness and dim timer, knob acceleration, spectrum zoom ×2/×4/×8, averaging, peak hold.
- **Spectrum**: zoom to ×64 with on-screen buttons, filled gradient trace and waterfall palettes, signal detection markers, seek and snap-to-signal (with filter fit), frequency tags from memories / RDS / a built-in list.
- **Recording**: recordings browser with playback through the radio's audio, and a UTC record timer (once or daily) that tunes, records and tunes back. Recordings are taken before the volume control.
- **Decoders**: RDS (PS / PTY / RT) on FM broadcast via optional `gr-rds`; APRS, POCSAG, FLEX, EAS and DTMF via `multimon-ng`.
- **Network mode**: hands the dongle the receiver is on to `rtl_tcp` on port 1234 and takes it back.
- **Two tuners**: the dongles are told apart by their tuner chips at start-up. The receiver uses the V3 for everything up to 1766 MHz (HF by direct sampling) and the XTR above that, reopening the other dongle when the frequency crosses over; a Tuner setting can hold it to one. The ADS-B decoder runs on the XTR beside the receiver, so the map can be open while listening on any band the V3 tunes, HF and CB included. The jobs were first the other way round (XTR main, V3 on ADS-B) and were swapped because the E4000 has nothing below about 65 MHz; `PISDR_MAIN_TUNER` / `PISDR_ADSB_TUNER` still give the old arrangement. One dongle on its own still works as before.
- **Location by ZIP code**: Settings → SYSTEM → Location ▸ takes a 5-digit US ZIP code on a number pad and looks it up in a bundled table (GeoNames, CC BY 4.0), with no network needed. It sets the location for the weather and the centre of the ADS-B map; AUTO returns to the IP address guess.
- **ADS-B aircraft map**: picking the ADS-B 1090 band starts `dump1090` on the XTR (the receiver plays on with the V3) and shows a radar scope laid out like the FR-360 flight radar, in the colours of the theme (range rings, sweep, coastlines and borders, aircraft silhouettes coloured by altitude, callsigns, trails), a flight data panel that steps through the aircraft by itself, and a sortable flight list with operator names from a bundled callsign table. `install.sh` builds the decoder; no service, no network ports.
- **Robustness**: settings, memories, tags and the scan log are saved atomically and damaged files are set aside as `*.corrupt` instead of being overwritten; each bad setting falls back on its own; a bug in a handler shows a message instead of closing the program; SIGTERM / poweroff saves and finalises a recording; recordings stop before the card fills.
- **Main screen in piHPSDR's layout**: VFO bar with a status line (mode and filter, AGC, NR, NB, notch, filters, squelch, decoders, bias tee, REC), VFO A and a second VFO B (A<>B, A>B), S-meter in S-units, MENU and HIDE buttons, frequency scale on top of the panadapter, a sliders area (AF, RF, squelch) with a pop-up slider when it is hidden, and a toolbar of three rows of keys behind an FNC key. A menu page reaches every page and Settings tab.
- **Test suite** (`software/tests/`, about 540 tests, PyQt5 and PyQt6) and a screenshot tool (`software/tools/screenshots.py`). See "Developing on a PC" in the README.
- **Fixes found by the tests**: BAND stuck on "HF utility" (and picking 40 m, 20 m, 31 m or WWV selected "HF utility" instead, so band scan covered 3–18 MHz); releasing a manual scan HOLD did not resume; a refused timer recording left the radio on the timer's frequency; "00:00-24:00" schedules were never on; the CW decoder printed stray `E`s on noise; a changed web feed or damaged cache could stop the HF conditions panel being built.

## 1. Using both tuners at once

The second dongle is there now. What is still missing is receiving on both together; today the receiver has one of them at a time and the other is idle unless the ADS-B map is open.

- **Scan on the XTR while listening on the V3.** Another `osmosdr.source` on the XTR that only hops and measures, with no second audio chain until a hit is handed to the listening radio. Two full demodulators and two waterfalls at once are more than a Pi 4 should run. It needs the back end to hold two sources (today a `Backend` has one `source`), a scanner that drives the second one, and a decision about what the screen shows for it.
- **ADS-B in the background.** Keep `dump1090` running on the XTR all the time, with the map as just a view of it, and show a count of aircraft on the main screen. The decoder costs part of a core continuously, so it should be a setting. The XTR would then never be free for the receiver, which only costs the range above 1766 MHz, and for a scan on the second dongle (the item above): the two exclude each other.
- **A better ADS-B front end.** The E4000 is not ideal at 1090 MHz and the XTR has no bias tee. A 1090 MHz filtered LNA with its own supply in front of it, or an R820T2 dongle in its place, would hear further.

Also still open from the hardware side:

- Antennas. The XTR wants the 1090 MHz antenna, the V3 the antenna you listen with; HF and VHF/UHF share the V3's one socket (a diplexer lets an HF and a VHF/UHF antenna both stay connected). See doc 01 section 4.
- A powered USB hub when a USB sound dongle is also plugged in: two dongles plus audio can exceed the Pi 4's shared USB current budget.
- The CAD in `cad/` and doc 03 still describe the single-dongle enclosure; the newer two-dongle enclosure is not in this repository.

An SDRplay RSPduo is the one-box alternative (two tuners, one USB device). It would replace the `rtl=N` source path with a SoapySDR source.

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
- **Dongles plugged in or out while running**: they are looked for once, at start-up. A dongle that disappears gives "Radio did not reopen"; one that appears is not noticed until PiSDR is restarted.
- **One PPM setting for two dongles**: both have a TCXO, so it has not mattered; a correction per dongle would be more correct.
- **Verify on hardware**: everything in "Robustness" and "Fixes" above, was checked on the simulator and the GNU Radio stand-in only. "Two tuners" has been run on the real dongles both ways round: first with the XTR as the main tuner and the V3 on ADS-B, and since the swap with the V3 as the main tuner and the XTR on ADS-B, which was reported working as intended (the dongles are found and given their jobs by their chips, and the aircraft map runs on the XTR while the receiver plays on the V3). Not individually reported from hardware: how many aircraft the XTR's E4000 hears compared with the V3 and where that unit's L-band gap starts, the move to the XTR at 1766 MHz, opening the map from above 1766 MHz, and the bias tee on the V3.
