# MeshCore BLE Field Flasher

*[Deutsche Version → README.de.md](README.de.md)*

A network-free, phone-driveable tool for flashing **MeshCore nRF52 nodes over Bluetooth Low
Energy** — RAK4631 by default, **any MeshCore board** (T114, T1000-E, WisMesh Tag, T-Echo, …) via
the web UI's *Target board* toggle — including nodes mounted high on a mast with no IP backhaul
(no WiFi, no LTE, no SSH at the site).

It started as a way to retire the iPhone as a DFU tool (nRF Connect for Mobile on iOS is painfully
slow and fails ~4 out of 5 times on legacy DFU). The result is a small, reliable kit that runs on a
laptop for the bench and on a **Raspberry Pi Zero 2 W** as a carried, self-contained field flasher
with a phone-browser web UI.

> **Heads-up:** this flashes the **Adafruit/OTAFix bootloader = legacy DFU** (app-only `.zip`), the
> bootloader MeshCore nRF52 builds ship with. It is *not* Nordic Secure DFU and does *not* need a
> Nordic dongle — it uses your host's onboard Bluetooth.

---

## Why this exists

- **iOS is the bottleneck.** nRF Connect on iOS 17+ stalls at random percentages (a PRN-default bug)
  and silently suspends DFU when the screen sleeps. Best case ~2.7 kB/s, typically 4/5 attempts fail.
- **Remote sites have no backhaul.** A repeater on a 20 m mast in the middle of nowhere has no
  internet — you cannot SSH a Pi in over the network. The flasher has to be *carried* to the site and
  driven locally, with zero network.
- **BLE is short-range.** Whatever the host, you need to be physically near the node. This kit is
  built around that constraint instead of fighting it.

## What you get

| Component | What it does |
|---|---|
| `bench_flash.py` | Flash a node over BLE and **measure** throughput (jump → bootloader → upload → kB/s). |
| `recover_flash.py` | Direct-to-bootloader flash for a node already stuck in DFU (e.g. after an aborted flash). No "jump" step — this is the un-brick path. |
| `ble_rssi_probe.py` | Measure RSSI / link margin to a target **before** risking a multi-minute flash. |
| `mc_serial.py` | Minimal MeshCore serial-CLI helper (trigger `start ota`, read `ver` / `public.key`). |
| `webflash.py` | **Phone-browser web UI** for the Pi — Flash / RSSI / Scan / Recover buttons, a firmware library, a **Target board** toggle (RAK4631-only or any MeshCore `*_OTA` board), and an unattended **Drone mode**. The flash runs server-side, so a slept/closed phone won't interrupt it. |
| `setup.sh` & friends | One-shot Pi provisioning: venv, auto-fallback field WiFi AP, captive portal, USB-NCM gadget, watchdog. |

## Hardware target

- **Node:** RAK4631 (Nordic nRF52840) running MeshCore with the Adafruit/OTAFix bootloader — the
  bench-proven reference target and the web UI's safe default.
- **Any other MeshCore nRF52 board** (T114, T1000-E, WisMesh Tag, T-Echo, Xiao, …): flip the web
  UI's **Target board** toggle to *any board* — it matches the universal `*_OTA` advertising-name
  suffix every MeshCore variant uses. The CLI scripts take `--name <BOARD>_OTA` for the same effect.
- **Host (bench):** any Windows / macOS / Linux machine with onboard BLE.
- **Host (field):** Raspberry Pi Zero 2 W (the 2 W — *not* the old Zero W; you want BT 4.2).

---

## Dependency: the DFU engine

The actual legacy-DFU protocol implementation is **[recrof/nrf_dfu_py](https://github.com/recrof/nrf_dfu_py)**,
a `bleak`-based client that explicitly lists the RAK4631 + Adafruit bootloader as tested. This repo's
scripts *wrap* it (orchestration, measurement, recovery, web UI, Pi provisioning) and expect it as a
sibling folder `nrf_dfu_py/`. It is **not vendored here** — clone it yourself:

```sh
cd flasher
git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git
```

On the Pi, `setup.sh` clones it for you automatically.

---

## Quick start — bench (laptop)

```sh
git clone https://github.com/ACETyr/meshcore-ble-field-flasher.git
cd meshcore-ble-field-flasher/flasher
git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git
python -m venv venv && . venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r ../requirements.txt
```

1. Put the node into OTA mode (advertises `RAK4631_OTA`):
   ```sh
   python mc_serial.py COM12 "start ota" 3        # Linux/Mac: /dev/ttyACM0
   ```
2. Flash and measure:
   ```sh
   python bench_flash.py /path/to/firmware.zip --retry 5 --verbose
   ```
3. If a flash aborts and the node is stuck advertising `4631_DFU`, recover it (no re-jump):
   ```sh
   python recover_flash.py /path/to/firmware.zip --retry 8
   ```

Full bench walkthrough: **[docs/BENCH.md](docs/BENCH.md)**.

## Quick start — field (Raspberry Pi Zero 2 W)

1. Flash Raspberry Pi OS Lite (64-bit), enable SSH, set your home WiFi for the *one-time* setup.
2. Copy the `flasher/` folder to the Pi and run setup (needs internet **once**):
   ```sh
   scp -r flasher <user>@pi-flasher.local:~/flasher
   ssh <user>@pi-flasher.local "bash ~/flasher/setup.sh"
   ```
   This installs everything under `/opt/flasher`, brings up a web UI service, and configures a field
   WiFi AP that **auto-starts whenever home WiFi is out of range** (so you can drive it with no network).
3. In the field: power the Pi, join its WiFi `pi-flasher`, open `http://10.42.0.1`, and flash.

Full Pi runbook: **[docs/PI-SETUP.md](docs/PI-SETUP.md)**.

### Web UI & Drone mode

Open `http://10.42.0.1` (field AP), `http://pi-flasher.local/` (home), or `http://10.55.0.1` (USB cable).

- **Firmware library** — upload / select / delete `.zip` DFU images in the browser (no SSH/SCP).
- **Target board** — off = RAK4631 only (exact `RAK4631_OTA` match, the safe default), on = any
  MeshCore board (every `*_OTA` advert; strongest RSSI wins). Applies to Flash, RSSI, Scan and
  Drone mode; persists across reboots.
- **Drone mode** — arm unattended, RSSI-gated auto-flash for when you mount the Pi on a pole or drone
  next to the mast and nobody is at the UI. Reboot-persistent arm state, auto-disarm timeout, and a
  flash history that proves what landed.

---

## Throughput & range — set expectations

- **~1.5 kB/s is the device-side floor** for this legacy DFU at a 20-byte MTU. It is bench-proven
  identical on Windows and on the Pi — the bootloader dictates the connection interval, so it is *not*
  host-tunable. A ~513 KB image takes ~6 minutes. That is fine: it runs **unattended**, so 6 minutes
  of "set and walk away" beats hours of babysitting a phone.
- **Do not use `--high-mtu`** on the OTAFix bootloader — it breaks the transfer. Leave it off.
- **Check RSSI first** with `ble_rssi_probe.py`. Want worst-case RSSI comfortably better than −80 dBm.
  If a mast node is marginal from the ground, a high-power directional BLE adapter (e.g. a 20 dBm USB
  adapter + an 18 dBi 2.4 GHz panel aimed up the mast) can close the link without a climb.

## Safety notes

- A failed flash is **not a brick** — the OTAFix bootloader falls back and keeps advertising
  `4631_DFU`; recover over BLE with `recover_flash.py`. This kit has been validated recovering exactly
  that state without physical access.
- **App-only DFU preserves identity** (the node's keypair in InternalFS). This repo deliberately ships
  no firmware and no erase/recovery images — you supply the `.zip` you want to flash.
- The Pi web UI is **unauthenticated**: the WiFi AP password is the only access control. **Change the
  default AP PSK** (`flashme123` in `setup.sh`) before any trip, and treat the AP as private.

---

## Credits

- DFU engine: [recrof/nrf_dfu_py](https://github.com/recrof/nrf_dfu_py)
- [MeshCore](https://github.com/meshcore-dev/MeshCore) and the RAK4631 hardware ecosystem
- Built and field-tested for a self-hosted MeshCore repeater network.

## License

[MIT](LICENSE) © 2026 Christoph Eder. Use at your own risk — see the disclaimer in the bench docs.
