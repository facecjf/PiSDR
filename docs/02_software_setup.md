# 02 - Software setup

Target: Raspberry Pi OS **64-bit, Desktop** (Debian 13 "Trixie" based, 2025 or later
image). Bookworm also works; the package names below are the same.

## 1. Flash and first boot

1. Raspberry Pi Imager -> Raspberry Pi 4 -> Raspberry Pi OS (64-bit). In the OS
   customisation dialog set the hostname (`pisdr`), a user, Wi-Fi and **enable SSH**.
2. Connect the Hosyond display (5 cm FFC, Pi bolted to the standoffs, pogo pins touching
   pins 2/4/6), insert the card, power up. The desktop should appear on the DSI screen
   with touch working - no driver installation is needed for this display.
3. If the image is upside down for your mounting, Preferences -> Screen Configuration ->
   right-click the DSI-1 output -> Orientation -> Inverted, then Apply. Touch rotates with it.
4. Update:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

## 2. RTL-SDR Blog V4 driver check

The V4 needs `librtlsdr` 2.0.1 or newer (osmocom) or the RTL-SDR Blog fork. Trixie ships
2.0.2, which includes V4 support. Verify instead of assuming:

```bash
sudo apt install -y rtl-sdr
# Stop the DVB kernel driver from grabbing the dongle
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo reboot
```

After the reboot, plug the V4 in and run:

```bash
rtl_test -t
```

Expected: a line containing `RTL-SDR Blog V4 Detected`, the tuner reported as `R828D`,
then sample statistics. Ctrl+C to stop.

If you see `R828D` but **no** "V4 Detected" line, or signals later appear at wrong
frequencies, the library is too old. Build the osmocom driver:

```bash
sudo apt purge -y ^librtlsdr rtl-sdr
sudo apt install -y libusb-1.0-0-dev git cmake build-essential debhelper pkg-config
cd ~ && git clone https://github.com/osmocom/rtl-sdr && cd rtl-sdr
sudo dpkg-buildpackage -b --no-sign
cd .. && sudo dpkg -i librtlsdr0_* librtlsdr-dev_* rtl-sdr_*
sudo apt-mark hold librtlsdr0 librtlsdr-dev rtl-sdr
sudo reboot
```

Then re-run `rtl_test -t`. Because `gr-osmosdr` links `librtlsdr0` dynamically, the apt
version of `gr-osmosdr` continues to work with the rebuilt library.

## 3. Install the application dependencies

```bash
sudo apt install -y gnuradio gr-osmosdr python3-numpy python3-pyqt5 python3-pyqtgraph \
                    python3-gpiozero python3-lgpio git qtwayland5 qt6-wayland libxcb-cursor0
```

`python3-pyqtgraph` may pull in PyQt6 and prefer it over PyQt5; both work. The two
`*wayland*` packages give each Qt generation its Wayland platform plugin, which the
Raspberry Pi OS desktop (labwc/Wayfire) needs. The console shows which binding was
chosen (`Qt binding: PyQt6`).

Check the pieces talk to each other:

```bash
python3 -c "from gnuradio import gr, blocks, filter, analog, audio; import osmosdr; print('gnuradio', gr.version())"
python3 -c "from PyQt5 import QtWidgets; import pyqtgraph; print('pyqtgraph', pyqtgraph.__version__)"
```

## 4. Install PiSDR

```bash
cd ~
git clone <this repository> PiSDR        # or copy the folder over with scp
cd PiSDR/software
./install.sh
```

`install.sh`:

- copies nothing; it runs the app from the repository folder so `git pull` updates it,
- writes `~/.config/autostart/pisdr.desktop` so the app starts full screen on login,
- adds the user to the `gpio` and `audio` groups,
- creates `~/.config/pisdr/` for `settings.json` and `~/Recordings/` for audio.

Run it by hand first, from the desktop session (so it can open the screen):

```bash
cd ~/PiSDR/software
python3 -m pisdr            # real radio, full screen
python3 -m pisdr --windowed # same, in an 800x480 window (useful over VNC)
python3 -m pisdr --sim      # no hardware, synthetic spectrum
```

"From the desktop session" means a terminal opened on the touchscreen or over VNC. A plain
SSH session has no display, and Qt fails with `could not connect to display`. To launch
from SSH anyway, point it at the logged-in desktop first:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 DISPLAY=:0
cd ~/PiSDR/software && python3 -m pisdr
```

(The user must already be logged in to the desktop on the Pi; `ls $XDG_RUNTIME_DIR` should
list `wayland-0`. If it lists `wayland-1` instead, use that name.)

`Ctrl+Q` or the EXIT soft key on the settings page quits.

## 5. Audio output

```bash
# list sinks
wpctl status
# make the headphone jack (or a USB dongle) the default
wpctl set-default <id>
wpctl set-volume @DEFAULT_AUDIO_SINK@ 90%
```

GNU Radio opens an ALSA device, so `wpctl` only affects it when ALSA's `default` device is
bridged to PipeWire:

```bash
sudo apt install pipewire-alsa
```

Without that bridge the app tries `default`, then `pipewire`, then `pulse`, then every card
`aplay -l` lists (USB dongle before the onboard jack) and uses the first one that opens.
Force one with `PISDR_AUDIO_DEVICE=plughw:CARD=Device,DEV=0` (the card id is the short name
in `aplay -l`). Sample rate is 48 kHz.

## 5. Settings page

Long-press **FUNC** (K3) opens Settings: two columns of grouped rows (RADIO, DISPLAY on
the left; AUDIO DSP, SYSTEM on the right). Tap a row to select it, tap it again or press
**CHANGE** to toggle/activate it, and turn the knob to step its value. **UP/DOWN** move
the selection for key-only use.

**Theme** (DISPLAY) switches the whole UI live and is remembered:

| Theme | Use |
|---|---|
| Amber | default dark, amber VFO / cyan highlights |
| Night | red-on-black for dark rooms, preserves night vision |
| Ice | cool blue/violet dark theme |
| Daylight | light, high contrast for bright rooms |

## 5a. Audio DSP (noise reduction / filters)

Settings rows under AUDIO DSP:

| Setting | What it does |
|---|---|
| Noise reduction | Spectral subtraction on demodulated audio (0 = off, 1–10) |
| Noise blanker | Blanks impulse spikes in the audio after demod (0 = off, 1–10) |
| Notch filter | Narrow audio band-reject; knob sets the notch frequency |
| Audio high-pass | Cuts rumble / hum (on by default at 250 Hz) |
| Audio low-pass | Cuts hiss above the cutoff |

CHANGE toggles on/off (or NR/NB between off and 5). The knob adjusts level or frequency
while a row is selected. Active NR / NB / notch show in the top status bar.

The filters are cheap IIR stages running in C++. NR and NB are Python blocks and are
only inserted into the audio path while enabled (you will hear a brief click as the
flowgraph restarts). If audio stutters with NR/NB on while the waterfall is showing,
lower **Spectrum rate** or turn them off; the default chain is unaffected.

## 5a-ii. CW (Morse) decoder

Select **CW** mode and a text strip appears under the frequency readout. Tune so the
station's tone sits inside the passband marker (the decoder listens at the 700 Hz CW
pitch, ±100 Hz). Decoded characters scroll in; the estimated WPM is shown at right and
**CLEAR** empties the buffer. Unknown patterns print as `*`.

Expectations: clean machine or keyer CW at 8–40 WPM decodes well; fading, QRM or sloppy
hand-sent code produces partial copy. Decoding runs on a 1 kHz envelope produced by C++
blocks, so it has no effect on audio quality. The simulator sends a looping `CQ` so you
can test the display without a dongle.

## 5b. Memories and scanning

Saved frequencies live in `~/.config/pisdr/memories.json` (created the first time you press
SAVE on the MEMORIES page). Soft keys on the main screen:

| Action | Control |
|---|---|
| Open memories | short press K4 (`MEM`) |
| Save / delete / recall | soft keys on the MEMORIES page |
| Start / stop scan | long press K4 |
| While scanning | `HOLD`, `SKIP`, `BAND`/`MEM` (scan source), `STOP` |
| Settings (incl. recording) | long press K3 (`FUNC`) |

**HF utility / numbers stations.** The **HF utility** band (3–18 MHz, USB) lands on
UVB-76 at 4625 kHz. Settings → SYSTEM → **Load HF presets** merges
`software/config/presets_numbers_stations.json` (UVB-76, The Pip, HM01 Cuba, E11/E07/S06
slots, WWV, CHU, VOLMET) into your memories without duplicating existing ones; then use
memory scan to catch a transmission. Edit that JSON to add your own. HF needs a real
antenna (long wire or loop) — the whip will hear almost nothing — and keep RF gain around
20–30 dB to avoid overload from broadcasters.

## 5c. HF conditions panel (solar / weather)

Below 30 MHz a panel appears to the right of the spectrum with the numbers that decide
whether HF is open: **SFI / SSN / A / K** tiles (K coloured green–amber–red), X-ray class,
solar wind speed and Bz, geomagnetic field and noise floor, the N0NBH **band-conditions**
table (80–40 m … 12–10 m, day and night), a 24-hour **Kp** bar strip, and the local weather
(storms are flagged red — lightning QRN). The age of the data sits top-right; it turns
amber with "offline" when the feeds can't be reached.

The **☀ SFI 92 K3** chip in the top bar always shows the latest SFI and K. Tap it to hide
the panel, or to pin it on while you're on VHF/UHF. Settings → SYSTEM → **HF conditions
panel** chooses `AUTO` (default: shown below 30 MHz), `ON` or `OFF`.

Data comes from `hamqsl.com` (hourly), NOAA SWPC (every 10 min) and Open-Meteo (every
15 min) over the Pi's Wi-Fi; no API keys. The last good values are cached in
`~/.config/pisdr/spacewx.json` so the panel is populated at boot even without a network.
Weather location defaults to IP geolocation; to fix it, add to `settings.json`:

```json
"wx_lat": 43.04, "wx_lon": -87.91, "wx_units": "imperial"
```

(`"metric"` gives °C and km/h.)

Band scan steps the current band by the tuning step and pauses when the S-meter is above
the higher of your squelch setting and −75 dB. Memory scan walks the saved list the same
way. Turning the knob or tapping the spectrum cancels the scan.

## 6. Headless CLI tune test (no UI)

Useful on the bench to prove the backend and driver before worrying about the screen:

```bash
cd ~/PiSDR/software
python3 -m pisdr.radio.flowgraph --freq 162.400e6 --mode NFM --gain 30 --seconds 20
```

You should hear the NOAA weather broadcast (US) or substitute a local FM broadcast
station with `--freq 100.1e6 --mode WFM`.

## 7. Autostart and kiosk behaviour

`install.sh` generates `software/pisdr-launch.sh` (waits up to 10 s for the dongle, then runs
`python3 -m pisdr`, logging to `~/.config/pisdr/autostart.log`) and registers it with the
compositor that Raspberry Pi OS actually uses:

| Desktop | Where it is registered |
|---|---|
| labwc (Trixie, later Bookworm) | a line in `~/.config/labwc/autostart` |
| Wayfire (early Bookworm) | `pisdr = ...` under `[autostart]` in `~/.config/wayfire.ini` |
| X11 / LXDE | `~/.config/autostart/pisdr.desktop` |

Check which one you have with `echo $XDG_CURRENT_DESKTOP` or `ps -e | grep -E 'labwc|wayfire'`
from a terminal on the desktop. If the app does not appear after a reboot, read
`~/.config/pisdr/autostart.log` - it contains the same errors you would see in a terminal.

- The app hides the mouse cursor itself (`Qt.BlankCursor`) and runs frameless at 800x480.
- To stop it auto-starting, remove the `# pisdr-autostart` lines from
  `~/.config/labwc/autostart` (or the `pisdr =` line from `wayfire.ini`) and
  `rm ~/.config/autostart/pisdr.desktop`.
- Logs: `~/.config/pisdr/pisdr.log` (rotating, 1 MB x 3) and `autostart.log`.

## 7a. "SIMULATOR" shown at startup

The app falls back to the simulated backend whenever the GNU Radio backend cannot be built,
and shows the reason in the orange banner. To see the full picture:

```bash
cd ~/PiSDR/software
python3 -m pisdr --check
```

This runs `rtl_test`, imports `gnuradio` and `osmosdr`, opens the dongle, builds the
demodulators, opens the audio device and constructs the backend, printing a traceback for
the first step that fails. Typical results:

| First failed step | Fix |
|---|---|
| `rtl_test`: no RTL-SDR found | Dongle not plugged in / bad extension cable; `lsusb` must list `0bda:2838` |
| `rtl_test`: usb_claim_interface | DVB driver holds it: section 2 blacklist, then reboot |
| `rtl_test`: not reported as V4 | Old `librtlsdr`; rebuild per section 2 |
| `gnuradio` / `gr-osmosdr` import | `sudo apt install gnuradio gr-osmosdr` (Python 3 bindings are included) |
| `audio` (`Unknown error 524`) | The SSH session cannot reach the desktop's PipeWire. Run the check from a terminal on the touchscreen, or `export XDG_RUNTIME_DIR=/run/user/$(id -u)` first. Then `wpctl status` and set a default sink (section 5) |

## 8. Known issues

| Symptom | Cause / fix |
|---|---|
| `rtl_test` says "usb_claim_interface error -6" | DVB kernel module grabbed the dongle; do the blacklist step and reboot |
| Spectrum is flat, no signals, "V4 Detected" missing | Old `librtlsdr`; rebuild per section 2 |
| Audio stutters | Reduce `SAMPLE_RATE` in `pisdr/config.py` to 1.024e6, or `FFT_FPS` to 15 |
| App starts on the HDMI monitor instead of the DSI | Disconnect HDMI, or set `PISDR_SCREEN=DSI-1` in the `.desktop` `Exec` line |
| "Undervoltage detected" in `dmesg` | Power supply or cable; see doc 01 section 5 |
