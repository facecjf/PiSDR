#!/usr/bin/env bash
# PiSDR installer for Raspberry Pi OS (Bookworm / Trixie, 64-bit Desktop).
# Installs apt dependencies, sets up groups and folders, and registers the autostart entry.
# The app itself runs from this folder so `git pull` is the upgrade path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

echo "== PiSDR install from $HERE for user $USER_NAME"

# dump1090 decodes the aircraft for the ADS-B map. It is built from source because Trixie has
# no package for it and the Bookworm package installs a service that takes the dongle at boot.
# Here it is only a program in /usr/local/bin that PiSDR starts while the map is open.
ADSB_TAG="v11.1"
build_adsb_decoder() {
  local src status=0
  src="$(mktemp -d)"
  sudo apt-get install -y build-essential pkg-config libncurses-dev git &&
    { pkg-config --exists librtlsdr || sudo apt-get install -y librtlsdr-dev; } &&
    git clone --quiet --depth 1 --branch "$ADSB_TAG" https://github.com/flightaware/dump1090.git "$src/dump1090" &&
    make -C "$src/dump1090" -j"$(nproc)" RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no SOAPYSDR=no \
      DUMP1090_VERSION="$ADSB_TAG" dump1090 &&
    sudo install -m 755 "$src/dump1090/dump1090" /usr/local/bin/dump1090-fa || status=$?
  rm -rf "$src"
  return "$status"
}

if [[ "${1:-}" != "--no-apt" ]]; then
  echo "== apt packages"
  sudo apt-get update
  sudo apt-get install -y \
    rtl-sdr gnuradio gr-osmosdr \
    python3-numpy python3-pyqt5 python3-pyqtgraph \
    python3-gpiozero python3-lgpio \
    qtwayland5 libxcb-cursor0 \
    multimon-ng
  # pyqtgraph may select PyQt6 if it is present; give Qt6 its Wayland plugin too.
  sudo apt-get install -y qt6-wayland 2>/dev/null || true
  # Optional: RDS station name / radio text on FM broadcast. PiSDR runs without it.
  sudo apt-get install -y gr-rds 2>/dev/null || echo "   gr-rds not available - RDS stays off"
  # Optional: ADS-B decoder for the aircraft map. PiSDR runs without it.
  if command -v dump1090-fa >/dev/null || command -v dump1090-mutability >/dev/null \
      || command -v readsb >/dev/null || command -v dump1090 >/dev/null; then
    echo "== ADS-B decoder already installed"
  else
    echo "== building the ADS-B decoder (dump1090 $ADSB_TAG, takes a few minutes)"
    build_adsb_decoder || echo "   dump1090 did not build - the ADS-B map stays unavailable"
  fi
fi

echo "== blacklisting the DVB kernel driver (needs reboot to take effect)"
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null

echo "== groups"
sudo usermod -aG gpio,audio,plugdev,video "$USER_NAME" || true

echo "== screen brightness permission (DSI / official touch displays)"
sudo tee /etc/udev/rules.d/99-pisdr-backlight.rules >/dev/null <<'RULE'
SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chgrp video /sys/class/backlight/%k/brightness", RUN+="/bin/chmod g+w /sys/class/backlight/%k/brightness"
RULE
for f in /sys/class/backlight/*/brightness; do
  [[ -e "$f" ]] && sudo chgrp video "$f" && sudo chmod g+w "$f"
done

echo "== folders"
mkdir -p "$HOME_DIR/.config/pisdr" "$HOME_DIR/Recordings" "$HOME_DIR/.config/autostart"

echo "== autostart"
LAUNCH="$HERE/pisdr-launch.sh"
sed "s#@SOFTWARE_DIR@#$HERE#g" "$HERE/pisdr-launch.sh.in" > "$LAUNCH"
chmod +x "$LAUNCH"
MARK="# pisdr-autostart"

# labwc (Raspberry Pi OS Trixie, and Bookworm after the labwc switch)
if [[ -d /etc/xdg/labwc ]] || command -v labwc >/dev/null; then
  mkdir -p "$HOME_DIR/.config/labwc"
  AUTO="$HOME_DIR/.config/labwc/autostart"
  touch "$AUTO"
  grep -q "$MARK" "$AUTO" || printf '\n%s\n%s &\n' "$MARK" "$LAUNCH" >> "$AUTO"
  echo "   registered in $AUTO"
fi

# Wayfire (early Bookworm)
if [[ -f "$HOME_DIR/.config/wayfire.ini" ]]; then
  WF="$HOME_DIR/.config/wayfire.ini"
  if ! grep -q "^pisdr *=" "$WF"; then
    grep -q "^\[autostart\]" "$WF" || printf '\n[autostart]\n' >> "$WF"
    sed -i "/^\[autostart\]/a pisdr = $LAUNCH" "$WF"
  fi
  echo "   registered in $WF [autostart]"
fi

# XDG autostart (X11 / LXDE sessions, and harmless elsewhere)
sed "s#@SOFTWARE_DIR@#$HERE#g" "$HERE/pisdr.desktop" > "$HOME_DIR/.config/autostart/pisdr.desktop"
chmod +x "$HOME_DIR/.config/autostart/pisdr.desktop"
echo "   registered in ~/.config/autostart/pisdr.desktop"

echo "== driver check"
if command -v rtl_test >/dev/null; then
  if timeout 5 rtl_test -t 2>&1 | grep -q "V4"; then
    echo "   RTL-SDR Blog V4 detected by librtlsdr - good."
  else
    echo "   WARNING: rtl_test did not report an RTL-SDR Blog V4."
    echo "   Either the dongle is not plugged in, the DVB driver still holds it (reboot),"
    echo "   or librtlsdr is too old - see docs/02_software_setup.md section 2."
  fi
fi

cat <<EOF

Done. Next:
  1. sudo reboot                         (applies the group + blacklist changes)
  2. cd $HERE && python3 -m pisdr --windowed   (first run from the desktop)
PiSDR will start full screen automatically on the next desktop login.
Autostart output goes to ~/.config/pisdr/autostart.log.
To disable autostart: remove the '$MARK' lines from ~/.config/labwc/autostart
(or the 'pisdr =' line in ~/.config/wayfire.ini) and rm ~/.config/autostart/pisdr.desktop
EOF
