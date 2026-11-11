# 05 - Features to add

The receiver today is one RTL-SDR Blog V4, one `osmosdr.source` at 1.92 MS/s, and one demodulator. `software/config/bands.json` only retunes that same radio.

## Done

Everything here runs on the single V4; user-facing details are in `docs/02_software_setup.md` sections 5–5e.

- **Band scan** (and memory scan): steps the current band, holds above a trigger level, resumes after it goes quiet. Dwell, resume (or stay) and trigger level are settings.
- **Scan lockouts** (long-press SKIP, or from the memories page), a **priority channel** checked every 2–10 s while scanning, and a **scan log** of every hold with a page to re-tune or lock out hits.
- **Memory groups** and UTC **schedules** (`utc`, `days`) with an ON NOW filter on the memories page and in memory scan. The numbers/military presets carry groups and the known schedules.
- **Receiver settings**: filter width per mode, PPM correction, S-meter calibration, CW pitch.
- **Display**: UTC clock, DSI backlight brightness and dim timer, knob acceleration, spectrum zoom ×2/×4/×8, averaging, peak hold.
- **Spectrum**: zoom to ×64 with on-screen buttons, filled gradient trace and waterfall palettes, signal detection markers, seek and snap-to-signal (with filter fit), frequency tags from memories / RDS / a built-in list.
- **Recording**: recordings browser with playback through the radio's audio, and a UTC record timer (once or daily) that tunes, records and tunes back. Recordings are taken before the volume control.
- **Decoders**: RDS (PS / PTY / RT) on FM broadcast via optional `gr-rds`; APRS, POCSAG, FLEX, EAS and DTMF via `multimon-ng`.
- **Network mode**: hands the dongle to `rtl_tcp` on port 1234 and takes it back.

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
