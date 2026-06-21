#!/usr/bin/env bash
# Pi Zero 2 W field-flasher setup — run ON the Pi after first boot (home WiFi = internet, ONCE).
# Creates /opt/flasher (nrf_dfu_py + our tools in a venv matching the Windows-tested bleak), a
# best-effort 7.5 ms BLE-interval unit, and a phone-driveable field WiFi-AP (no internet needed).
# Re-runnable. Run it from inside the copied pi-setup/ folder:  bash setup.sh
set -e
SELF="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/flasher

echo "== apt deps =="
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip rfkill network-manager bluez

echo "== bring up onboard Bluetooth (fresh Lite images soft-block it via rfkill) =="
sudo rfkill unblock bluetooth || true
sudo systemctl enable --now bluetooth 2>/dev/null || true
sudo usermod -aG dialout,bluetooth "$USER" || true   # serial + BLE access (re-login to take effect)

echo "== $DEST + nrf_dfu_py + our tools =="
sudo mkdir -p "$DEST"
sudo chown "$USER:$USER" "$DEST"
[ -d "$DEST/nrf_dfu_py" ] || git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git "$DEST/nrf_dfu_py"
cp "$SELF"/bench_flash.py "$SELF"/recover_flash.py "$SELF"/mc_serial.py "$SELF"/ble_rssi_probe.py \
   "$SELF"/webflash.py "$DEST"/
# Firmware library dir (web UI uploads/selects images here). Seed it from a bundled firmware.zip.
mkdir -p "$DEST/firmware"
[ -f "$SELF/firmware.zip" ] && cp "$SELF/firmware.zip" "$DEST/firmware/firmware.zip" && echo "  seeded firmware/firmware.zip"
# Default config (active image + drone-mode defaults). Don't clobber an existing one on re-run.
[ -f "$DEST/config.json" ] || cat > "$DEST/config.json" <<'EOF'
{
  "active_firmware": "firmware.zip",
  "autoflash": { "rssi_threshold_dbm": -80, "arm_timeout_min": 30, "cooldown_sec": 120 },
  "runtime": { "armed": false }
}
EOF

echo "== python venv (bleak + pyserial + flask, matches the bench-tested versions) =="
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --upgrade pip bleak pyserial flask

echo "== web UI service (phone browser → flash; survives browser disconnects) =="
sudo cp "$SELF/webflash.service" /etc/systemd/system/webflash.service
sudo systemctl daemon-reload
sudo systemctl enable webflash.service
sudo systemctl restart webflash.service   # restart (not just enable --now) so a re-run loads new code
echo "  web UI on http://<pi-ip>/  (field AP: http://10.42.0.1 · USB gadget: http://10.55.0.1)"

echo "== best-effort 7.5 ms BLE connection interval (the throughput lever) =="
sudo tee /etc/systemd/system/ble-fast-interval.service >/dev/null <<'EOF'
[Unit]
Description=Request 7.5ms BLE connection interval (best-effort, BlueZ may yield to peripheral)
After=bluetooth.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo 6 > /sys/kernel/debug/bluetooth/hci0/conn_min_interval; echo 6 > /sys/kernel/debug/bluetooth/hci0/conn_max_interval'
[Install]
WantedBy=bluetooth.service
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ble-fast-interval.service 2>/dev/null || \
  echo "  (interval unit enabled; will apply on next boot / when hci0 is up)"

echo "== hardware watchdog (unattended/drone resilience: a fully hung Pi self-reboots) =="
# bcm2835_wdt is present on the Pi; systemd pets it. A hang -> reboot; the drone arm-state is
# reboot-persistent (config.json runtime.armed) so the mission resumes. webflash.service already
# Restart=on-failure. daemon-reexec applies the new RuntimeWatchdogSec without a reboot.
sudo mkdir -p /etc/systemd/system.conf.d
sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=15
EOF
sudo systemctl daemon-reexec 2>/dev/null || echo "  (watchdog drop-in written; applies on next boot)"

echo "== field WiFi AP — AUTO-FALLBACK when home WiFi is absent (no manual switch, no SSH needed) =="
# CHANGE the psk! The AP auto-activates whenever no higher-priority client WiFi connects (= in the field).
sudo nmcli con add type wifi ifname wlan0 con-name field-ap ssid pi-flasher \
  802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "flashme123" \
  connection.autoconnect yes connection.autoconnect-priority 0 2>/dev/null || \
  echo "  (field-ap profile already exists — ok)"
# Home WiFi wins whenever it's in range (higher priority) → client at home, AP fallback in the field.
HOME_WIFI=$(nmcli -t -f NAME,TYPE con show --active | awk -F: '$2 ~ /wireless/ {print $1}' | head -1)
[ -n "$HOME_WIFI" ] && sudo nmcli con mod "$HOME_WIFI" connection.autoconnect yes \
  connection.autoconnect-priority 100 && echo "  home WiFi '$HOME_WIFI' prio=100, field-ap prio=0"

echo "== captive portal (phone auto-opens the flasher when it joins the AP) =="
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo cp "$SELF/dnsmasq-captive.conf" /etc/NetworkManager/dnsmasq-shared.d/captive.conf
sudo systemctl reload NetworkManager 2>/dev/null || true

cat <<EOF

================ DONE ================
WEB UI (primary interface) — open a browser:
  • field:  power the Pi, join WiFi 'pi-flasher' (psk flashme123) → http://10.42.0.1
  • home:   http://pi-flasher.local/  or the Pi's home-WiFi IP
  • USB:    bash $DEST/enable-usb-gadget.sh  (once) + reboot → http://10.55.0.1
  Buttons: Flash · RSSI/range · Scan · Recover. The job runs SERVER-SIDE → a slept/closed phone
  won't interrupt it; just reload the page to see the live state.
  Firmware library: upload/select DFU images right in the web UI (no SSH/SCP).
  Drone mode: a switch that arms unattended auto-flash (RSSI-gated, reboot-persistent, auto-disarm
  timeout) — for putting the Pi on a pole/drone next to the mast when ground range is too short.

Field procedure (manual): RF-admin 'start ota' on the node → web UI → Scan/RSSI (range) → Flash.
Field procedure (drone):  arm Drone mode → mount Pi by the mast → RF-admin 'start ota' → it flashes
  itself; the flash history (and /flashlog) proves it landed.

CLI fallback (same engine; web UI is the primary path):
  $DEST/venv/bin/python3 $DEST/bench_flash.py $DEST/firmware/firmware.zip --retry 8
  $DEST/venv/bin/python3 $DEST/recover_flash.py $DEST/firmware/firmware.zip --retry 8   # node stuck in 4631_DFU
Update firmware: upload it in the web UI (firmware library), or copy a zip into $DEST/firmware/.
NOTE: single 2.4 GHz radio shares WiFi-AP + BLE (coexistence) — fine for control.
EOF
