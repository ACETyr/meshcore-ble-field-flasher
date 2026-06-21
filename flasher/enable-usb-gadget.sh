#!/usr/bin/env bash
# Enable a USB CDC-NCM ethernet gadget on the Pi Zero 2 W so a host (Windows 11 / tablet / Mac / Linux)
# reaches it over a single USB cable — no WiFi. The host gets DHCP, browses http://10.55.0.1 (web UI) and
# can ssh <user>@10.55.0.1. Makes the flasher directly maintainable + tablet-attachable.
#
# Why NCM (not RNDIS): Microsoft disabled legacy-RNDIS auto-install on Win10/11; g_ether shows as an
# un-drivered device. Windows 11 has a NATIVE CDC-NCM driver (UsbNcm.sys) that auto-binds. NCM also works
# on Linux/macOS/Android. (Verified 2026-06-18: "UsbNcm Host Device" Up, host got 10.55.0.127, web UI OK.)
#
# Plug the cable into the Pi's USB DATA port (inner micro-USB, NOT PWR). Run once, then reboot.
# ⚠️ Edits /boot/firmware/cmdline.txt (line 1 only, idempotent). Recovery if it won't boot: pull the SD
#    into a PC and fix cmdline.txt on the BOOTFS partition.
set -e
SELF="$(cd "$(dirname "$0")" && pwd)"
CFG=/boot/firmware/config.txt;  [ -f "$CFG" ] || CFG=/boot/config.txt
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt

echo "== config.txt: dwc2 overlay (peripheral/gadget) =="
# NOTE: stock config.txt has 'dtoverlay=dwc2,dr_mode=host' under [cm5] (Compute Module only, does NOT
# apply to a Pi Zero). Match our SPECIFIC peripheral line so we aren't fooled into skipping. Appending to
# EOF lands it under the last section ([all] in the stock file) = applies to the Pi Zero.
grep -q 'dtoverlay=dwc2,dr_mode=peripheral' "$CFG" || \
  echo 'dtoverlay=dwc2,dr_mode=peripheral' | sudo tee -a "$CFG" >/dev/null

echo "== cmdline.txt: load dwc2 controller (NO gadget module — libcomposite NCM binds it) =="
# remove any legacy g_ether we may have set before; ensure dwc2 is loaded
sudo sed -i '1 s/modules-load=dwc2,g_ether/modules-load=dwc2/' "$CMD"
grep -q 'modules-load=dwc2' "$CMD" || sudo sed -i '1 s/$/ modules-load=dwc2/' "$CMD"

echo "== install the CDC-NCM gadget script + service =="
sudo cp "$SELF/gadget-ncm.sh" /opt/flasher/gadget-ncm.sh
sudo chmod +x /opt/flasher/gadget-ncm.sh
sudo cp "$SELF/usb-gadget-ncm.service" /etc/systemd/system/usb-gadget-ncm.service
sudo systemctl daemon-reload
sudo systemctl enable usb-gadget-ncm.service

echo "== NetworkManager profile for usb0 (host gets DHCP, Pi = 10.55.0.1) =="
sudo nmcli con add type ethernet ifname usb0 con-name usb-gadget \
  ipv4.method shared ipv4.addresses 10.55.0.1/24 autoconnect yes \
  connection.autoconnect-priority 50 2>/dev/null || \
  echo "  (usb-gadget profile already exists — ok)"
# stop NM's generic wired profile from grabbing usb0
sudo nmcli con mod netplan-eth0 connection.autoconnect no 2>/dev/null || true

echo
echo "DONE. Reboot:  sudo reboot"
echo "Then connect the Pi's USB DATA port to the host. Windows 11 auto-loads 'UsbNcm Host Device'."
echo "Reach it at:  http://10.55.0.1   and   ssh <user>@10.55.0.1"
