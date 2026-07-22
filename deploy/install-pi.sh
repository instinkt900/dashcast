#!/usr/bin/env bash
# Install the dashcast DISPLAY side on the Raspberry Pi.
# Run ON the Pi (e.g. after rsync'ing the repo there):  sudo bash deploy/install-pi.sh
#
# This converts the Pi from a Chromium kiosk (FullPageOS) into a lightweight
# framebuffer image display. It:
#   - installs the dashcast package to /opt/dashcast
#   - installs config to /etc/dashcast/config.toml (won't overwrite an existing one)
#   - ensures `fbi` is present
#   - stops the FullPageOS X/Chromium kiosk (disables lightdm, boots to console)
#   - installs and enables the dashcast-display systemd service
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "please run as root (sudo)"; exit 1
fi

echo "==> installing dashcast package to /opt/dashcast"
install -d /opt/dashcast
cp -r "$REPO_DIR/dashcast" /opt/dashcast/

echo "==> installing config to /etc/dashcast"
install -d /etc/dashcast
if [[ -f /etc/dashcast/config.toml ]]; then
  echo "    /etc/dashcast/config.toml exists — leaving it untouched"
else
  cp "$REPO_DIR/config.example.toml" /etc/dashcast/config.toml
  echo "    wrote /etc/dashcast/config.toml — EDIT the [display] image_url before starting"
fi

echo "==> installing offline notice box (if present in repo)"
if [[ -f "$REPO_DIR/deploy/assets/offline_box.fb" ]]; then
  cp "$REPO_DIR/deploy/assets/offline_box.fb" /etc/dashcast/offline_box.fb
  echo "    installed /etc/dashcast/offline_box.fb"
else
  echo "    no offline_box.fb in repo — generate with: dashcast make-offline -c config.toml -o deploy/assets/offline_box.fb"
fi

echo "==> disabling FullPageOS Chromium kiosk (lightdm) and booting to console"
if systemctl is-enabled lightdm >/dev/null 2>&1; then
  systemctl disable --now lightdm || true
fi
systemctl set-default multi-user.target

echo "==> freeing tty1 (disable getty) so it doesn't draw over the framebuffer"
systemctl disable --now getty@tty1.service 2>/dev/null || true

echo "==> installing systemd service"
cp "$REPO_DIR/deploy/dashcast-display.service" /etc/systemd/system/dashcast-display.service
systemctl daemon-reload
systemctl enable dashcast-display.service

cat <<EOF

Done. Next:
  1. Edit /etc/dashcast/config.toml  (set [display] image_url to the server)
  2. Start it:   sudo systemctl start dashcast-display
  3. Watch logs: journalctl -u dashcast-display -f

To revert to the FullPageOS kiosk:
  sudo systemctl disable --now dashcast-display
  sudo systemctl enable --now getty@tty1
  sudo systemctl enable --now lightdm
  sudo systemctl set-default graphical.target
EOF
