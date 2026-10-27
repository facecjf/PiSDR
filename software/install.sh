#!/usr/bin/env bash
# PiSDR installer for Raspberry Pi OS (Bookworm / Trixie, 64-bit Desktop).
# Installs apt dependencies, sets up groups and folders, and registers the autostart entry.
# The app itself runs from this folder so `git pull` is the upgrade path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

echo "== PiSDR install from $HERE for user $USER_NAME"

if [[ "${1:-}" != "--no-apt" ]]; then
  echo "== apt packages"
  sudo apt-get update
  sudo apt-get install -y \
    rtl-sdr gnuradio gr-osmosdr \
    python3-numpy python3-pyqt5 python3-pyqtgraph \
    python3-gpiozero python3-lgpio \
    qtwayland5 libxcb-cursor0
  # pyqtgraph may select PyQt6 if it is present; give Qt6 its Wayland plugin too.
  sudo apt-get install -y qt6-wayland 2>/dev/null || true
fi

echo "== blacklisting the DVB kernel driver (needs reboot to take effect)"
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null

echo "== groups"
sudo usermod -aG gpio,audio,plugdev "$USER_NAME" || true

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
