# Raspberry Pi Zero 2 W — self-contained BLE field-flasher

Goal: a network-free, phone-driveable Pi that BLE-flashes MeshCore RAK4631 nodes on-site (no
SSH/NetBird backhaul needed at the mast), plus measures BLE range/throughput.

Use a **Pi Zero 2 W** (BT 4.2), not the original Zero W.

---

## Step 1 — Flash the SD card

Use Raspberry Pi Imager.

- **OS:** `Raspberry Pi OS (other)` → **Raspberry Pi OS Lite (64-bit)** (the Zero 2 W is ARMv8;
  Lite = headless).
- **Edit Settings (OS customisation):**
  - **Hostname:** `pi-flasher` → reachable as `pi-flasher.local`
  - **Enable SSH** → password authentication
  - **Username / Password:** pick your own (used for SSH below as `<user>`)
  - **Wireless LAN:** your *home* SSID + password (only for the one-time setup at home; the field uses
    the Pi's own AP). Set the correct Wireless LAN country.
  - **Locale:** your time zone + keyboard layout
- Write, verify, eject.

## Step 2 — First boot + find the Pi (at home, on your WiFi)

Insert the SD, power the Pi via its **PWR** port, wait ~60–90 s, then:

```sh
ssh <user>@pi-flasher.local
```

If `.local` doesn't resolve, find the Pi's IP on your router and `ssh <user>@<ip>`.

## Step 3 — Deploy the flasher kit (one-time, needs home internet)

From your machine, copy the `flasher/` folder to the Pi and run setup. Optionally drop a firmware
`.zip` into `flasher/firmware.zip` first to seed the firmware library:

```sh
scp -r flasher <user>@pi-flasher.local:~/flasher
ssh <user>@pi-flasher.local "bash ~/flasher/setup.sh"
```

`setup.sh` installs git/venv, clones `nrf_dfu_py`, builds a venv with `bleak`+`pyserial`+`flask`,
installs the web UI service, adds a best-effort 7.5 ms BLE-interval unit and a hardware watchdog, and
creates the field WiFi-AP profile. Everything lands in `/opt/flasher/`.

> **Change the AP PSK** (`flashme123`) in `setup.sh` before you run it, or right after — the web UI is
> unauthenticated and the AP password is the only access control.

## Step 4 — Home throughput test (optional)

Plug the RAK4631 devboard into the Pi's **USB DATA** port (shows as `/dev/ttyACM0`):

```sh
ssh <user>@pi-flasher.local
/opt/flasher/venv/bin/python3 /opt/flasher/mc_serial.py /dev/ttyACM0 "start ota" 3
/opt/flasher/venv/bin/python3 /opt/flasher/bench_flash.py /opt/flasher/firmware/firmware.zip --retry 5
```

Expect ~1.5 kB/s (the device floor). high-MTU stays OFF (it breaks OTAFix).

## Step 5 — Field (no internet): drive the Pi from your phone

The field AP **starts automatically** when home WiFi is out of range (autoconnect priority: home WiFi
100, field AP 0). No SSH or switch needed — this solves the no-backhaul deadlock (you can't SSH in to
enable the AP if there's no network).

Verify the fallback **at home before the trip**: `sudo reboot`, wait ~90 s, and confirm the Pi rejoins
home WiFi. If it does, the priority is correct (home wins when present ⇒ the AP auto-starts when home
is absent).

At the mast:

1. Power the Pi.
2. On your phone, join WiFi **`pi-flasher`** (your PSK).
3. Open the web UI at `http://10.42.0.1` (a captive-portal redirect usually pops it automatically), or
   `ssh <user>@10.42.0.1` from a phone SSH app.
4. Put the node in OTA mode — via RF-admin `start ota` over the mesh from a carried admin node, or over
   USB if you can reach it.
5. **Check range first** (don't burn minutes on a marginal link):
   ```sh
   /opt/flasher/venv/bin/python3 /opt/flasher/ble_rssi_probe.py RAK4631_OTA
   ```
   Want worst-case RSSI comfortably better than −80 dBm. If marginal, use a high-power directional BLE
   adapter aimed up the mast (e.g. a 20 dBm USB adapter + an 18 dBi 2.4 GHz panel).
6. **Flash:**
   ```sh
   /opt/flasher/venv/bin/python3 /opt/flasher/bench_flash.py /opt/flasher/firmware/firmware.zip --retry 8
   ```
   A failed flash is not a brick — the node sits in `4631_DFU`; recover with
   `recover_flash.py /opt/flasher/firmware/firmware.zip --retry 8`.

The web UI does all of this with buttons (Scan · RSSI · Flash · Recover) — the CLI is the fallback.

## Step 6 — Web UI: firmware library + Drone mode

Open `http://10.42.0.1` (field AP), `http://pi-flasher.local/` (home), or `http://10.55.0.1` (USB
gadget — see below).

- **Firmware library** — upload `.zip` DFU images in the page (no SSH/SCP), pick the **active** one
  with the radio buttons, delete old ones. Uploads are validated as real DFU packages; non-app-only
  images (softdevice/bootloader) are flagged ⚠ (riskier — they can disturb identity/FS). Flash and
  Drone mode always use the active image. Carry multiple builds and switch on site.
- **Target board** (the switch) — off = flash **RAK4631 only** (exact `RAK4631_OTA` match; the safe
  default), on = **any MeshCore board**: matches every `*_OTA` advert (T114_OTA, T1000E_OTA, TECHO_OTA,
  …) and any DFU-service bootloader. Applies to manual Flash, RSSI probe, Scan markers AND Drone mode;
  persists across reboots. ⚠ In any-board mode make sure the active image matches the board you're
  targeting — the flasher cannot tell a T114 image from a RAK image beyond the DFU manifest.
- **Drone mode** (the switch) — for when ground→mast BLE range is too short and the Pi must ride a pole
  or drone next to the node, with nobody at the UI:
  1. In *auto-flash settings* set the **RSSI threshold** (default −80 dBm; only a node at least this
     strong is flashed → the nearby intended one, not a distant mesh node), **arm timeout** (auto-disarm
     after N min; 0 = never), and **per-node cooldown**.
  2. Flip **Drone mode → On**. The switch is colour-coded: amber = armed/scanning · blue = flashing ·
     green = last OK · red = last fail.
  3. Mount the Pi by the mast, then put the node in OTA mode (RF-admin `start ota` from the ground).
     The Pi detects it and flashes the active image by itself. A flash that fails leaves the node in
     `4631_DFU` → the loop auto-recovers it on the next pass.
  4. The arm state is **reboot-persistent** (a power blip mid-mission resumes armed; the timeout
     restarts). Disarm any time with the switch — an in-flight flash finishes first.
  5. Verify it actually flashed via the **flash history** panel (or `GET /flashlog`): one line per
     attempt with target MAC, result, and kB/s.

### USB-cable access (no WiFi)

To reach the Pi over a single USB cable (host gets DHCP, browse `http://10.55.0.1`):

```sh
bash /opt/flasher/enable-usb-gadget.sh   # once
sudo reboot
```

This sets up a **CDC-NCM** ethernet gadget (Windows 11 has a native driver; also Linux/macOS/Android).
Plug the cable into the Pi's USB **DATA** port (the inner micro-USB, *not* PWR).

### ⚠ Before any field/drone trip

- **Change the AP PSK** — the web UI is unauthenticated; anyone who joins the AP can upload firmware
  and arm Drone mode. Treat the AP as the only access control; keep it private.
- The Pi has **no RTC and no NTP in the field**: the page seeds the clock from your phone on first load
  so history timestamps are real; every entry also carries a monotonic `ts_uptime`.
- All `RAK4631_OTA` adverts are identical → the **RSSI gate is the only nearby-vs-distant filter**.
  Make sure only the intended node is the strongest in BLE range, or raise the threshold.

---

### Notes

- Single 2.4 GHz radio → WiFi-AP + BLE coexist (time-shared). Fine for control; for the cleanest
  throughput number, test once with the AP down.
- Update field firmware by uploading it in the web UI (firmware library) — at home or over the field AP
  from your phone. No internet needed to flash, only the `.zip` on the SD.
- `start ota` on the real node requires firmware that supports it (verify once via RF-admin →
  `OK - mac: …`). On a bench devboard over USB, `start ota` needs no password.
