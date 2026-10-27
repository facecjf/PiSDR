# Pi SDR — installation and assembly sequence (HF-RX 04)

## Before printing
1. Confirm actual Hosyond panel dimensions, DSI connector location, ribbon route, mounting hardware and LCD thickness.
2. Power the Pi 4 with quality 5.1V/3A supply; connect Hosyond via DSI and its manufacturer-approved power arrangement; verify touch and video.
3. Dry-fit Pi/screen stack, RTL-SDR dongle and USB cable; verify connectors and cooling clearance inside 208×188×78 mm shell.
4. Print one small aperture/control-hole test before the whole enclosure; check prints within 220×220 mm bed.

## Install Raspberry Pi OS and Gqrx
1. Flash Raspberry Pi OS (64-bit, Desktop), boot, update it, and confirm Hosyond display works.
2. Run:
   ```bash
   sudo apt update
   sudo apt full-upgrade -y
   sudo apt install -y gqrx-sdr rtl-sdr python3-gpiozero
   ```
3. **Verify RTL-SDR Blog V4 compatibility.** If `rtl_test -t` or Gqrx fails, use official V4 Linux driver instructions at https://www.rtl-sdr.com/v4/ . Do not blindly remove library files on an existing system. Confirm which version of `librtlsdr` Gqrx actually loads and restart it after changes.
4. Ensure no competing DVB driver or app has grabbed the RTL device. With Gqrx closed, run `rtl_test -t` and stop with Ctrl+C after detection.
5. Start `gqrx`; choose RTL-SDR (`rtl=0`), direct sampling OFF, LNB offset 0, bias tee OFF, start 1.024MS/s. In AM mode test 10 MHz WWV when conditions permit.

## Connect GPIO controls
See `../DIAGRAMS/gpio_pinout.png` and the table in `PROJECT_SETTINGS.md`.
- All inputs use internal pullups to **3.3 V** and switch to GND; no external 5V input.
- Use bare EC11 pins A/B/common and separate button contacts; label wires.
- With Gqrx open, Tools -> Remote control -> start TCP server on localhost port 7356.
- From the `SOFTWARE` folder run:
   ```bash
   python3 hf_controls.py
   ```
- Confirm turn direction, push-to-change-step, BAND, MODE, STEP and REC. If reversed, swap A/B encoder inputs or swap callbacks in Python.

## RF and safety
- Begin testing with a safe, modest receive antenna. Keep long wire away from overhead electrical wires.
- For outdoor feedline use building-code-compliant entry protection and grounding; the 9:1 UNUN/ESD circuitry isn't lightning protection.
- Leave the V4's 4.5V bias tee **OFF** with passive receiving wire/UNUN.
- Never connect a radio transmitter output to SDR antenna input.

## Bench test -> print -> final assembly
1. Make sure Pi is cooled, display/touch work, RTL device appears, actual HF signals can be tuned, and Gqrx remote server responds.
2. Verify mount geometry and clearance, print faceplate + rear shell + knob + clips.
3. Mount LCD without placing force on display glass; mount Pi, use shielded USB extension, maintain airflow, secure SMA bulkhead and coax.
4. Check cable strain relief and accessible power button/USB ports; close enclosure and run a 30-minute thermal/reception test.

## Known limitations / work remaining
- This release is a CAD prototype, not fit-verified or assembled.
- Exact screen-retainer adjustment, port cutouts, antenna entry and Pi mounting posts require confirmation on your physical hardware.
- Receiver/control software integration has not been tested here on actual Pi.
- If Gqrx on your Pi does not work with the V4, use updated manufacturer driver; do not configure Q-branch direct sampling or -125 MHz upconverter offset (both relate to old receiver designs).
