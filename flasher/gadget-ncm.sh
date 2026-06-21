#!/usr/bin/env bash
# CDC-NCM USB ethernet gadget via libcomposite/configfs. Windows 11 has a NATIVE NCM class driver
# (UsbNcm.sys) — unlike legacy RNDIS, whose auto-install Microsoft disabled. Also works on Linux,
# macOS, Android. Creates the usb0 interface; NetworkManager's 'usb-gadget' profile gives it 10.55.0.1.
# Run as a oneshot systemd service at boot (after dwc2 is loaded). Idempotent.
set -e
G=/sys/kernel/config/usb_gadget/meshflasher
modprobe libcomposite || true

# wait for the dwc2 UDC to appear
UDC=""
for i in $(seq 1 60); do UDC=$(ls /sys/class/udc 2>/dev/null | head -1); [ -n "$UDC" ] && break; sleep 0.25; done
[ -z "$UDC" ] && { echo "gadget-ncm: no UDC found"; exit 1; }

[ -d "$G" ] && { echo "gadget-ncm: already configured"; exit 0; }

mkdir -p "$G"; cd "$G"
echo 0x1d6b > idVendor              # Linux Foundation
echo 0x0104 > idProduct             # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB
# Misc / IAD so Windows parses the CDC-NCM function classes
echo 0xEF > bDeviceClass
echo 0x02 > bDeviceSubClass
echo 0x01 > bDeviceProtocol
mkdir -p strings/0x409
echo "meshflasher01" > strings/0x409/serialnumber
echo "MeshCore"      > strings/0x409/manufacturer
echo "Pi Field Flasher" > strings/0x409/product
mkdir -p configs/c.1/strings/0x409
echo "NCM" > configs/c.1/strings/0x409/configuration
echo 250  > configs/c.1/MaxPower
mkdir -p functions/ncm.usb0
ln -s functions/ncm.usb0 configs/c.1/
echo "$UDC" > UDC
echo "gadget-ncm: CDC-NCM bound to $UDC"
