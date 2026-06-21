# Bench flashing — BLE-flash a RAK4631 from a laptop

Goal: prove `nrf_dfu_py` over your host's onboard BLE flashes the RAK4631 reliably, and measure
throughput. This is the workflow you validate on the bench before trusting the Pi field-flasher.

## Prerequisites

- A host (Windows / macOS / Linux) with onboard Bluetooth LE.
- Python 3.10+ with the deps from `requirements.txt` (`bleak`, `pyserial`).
- The DFU engine cloned next to the scripts:
  ```sh
  cd flasher
  git clone --depth 1 https://github.com/recrof/nrf_dfu_py.git
  ```
- A firmware `.zip` to flash — an **app-only legacy-DFU package** (`dfu_version 0.5`, S140 v6). A
  MeshCore RAK4631 repeater build produces exactly this `firmware.zip` in its PlatformIO build dir.
- The RAK4631 connected over USB as a serial console (e.g. `COM12` on Windows, `/dev/ttyACM0` on
  Linux/macOS). All `mc_serial.py` examples below use `COM12` — substitute your port.

> **Tip:** flashing a `_debug` build makes the result unambiguous — the debug image prints `RAW:` /
> `fwd-filter:` lines on serial, so you can see at a glance that the new firmware is running.

## Step 1 — Baseline identity (to prove the flash took, and that identity was preserved)

```sh
python mc_serial.py COM12 "ver" 1.5
python mc_serial.py COM12 "get public.key" 2
```

Note the `ver` string and the public key. App-only DFU preserves identity, so `public.key` **must** be
identical after flashing.

## Step 2 — Put the board into OTA mode

```sh
python mc_serial.py COM12 "start ota" 3
```

Expect `OK - mac: XX:XX:XX:XX:XX:XX`. The device now advertises **`RAK4631_OTA`** over BLE. Leave USB
plugged in — it stays a serial console so you can re-read `ver` after the flash. (If it asks for a
password, run `python mc_serial.py COM12 "password <pw>" 1.5` first, then retry `start ota`.)

## Step 3 — Flash over BLE and measure

```sh
python bench_flash.py /path/to/firmware.zip --retry 5 --verbose
```

Watch: scan → "Found RAK4631_OTA" → "Jump command sent" → "Scanning for Bootloader" → "Uploading: N%"
→ **`=== RESULT: SUCCESS — … = X.XX kB/s ===`**.

If it stalls or errors: reduce PRN (`--prn 4` or `--prn 1`) or raise the start delay (`--delay 0.6`).
A failed flash is **not a brick** (the OTAFix bootloader auto-falls-back) — re-issue `start ota`
(Step 2) and retry, or use the recovery path below.

> **Do not pass `--high-mtu`.** It breaks the OTAFix bootloader (the connection drops partway). Leave
> it off; throughput is bootloader-limited regardless.

## Step 4 — Verify

After the device reboots (the serial port re-enumerates; give it ~10 s):

```sh
python mc_serial.py COM12 "ver" 2
python mc_serial.py COM12 "get public.key" 2
python mc_serial.py COM12 "" 5
```

- `public.key` unchanged → identity preserved. ✅
- The listen-only command should show `RAW:` / RX debug logging if you flashed a debug image → confirms
  the new firmware is running. ✅

## Recovery — node stuck in the bootloader

If a flash aborts, the node sits in the OTAFix bootloader advertising `4631_DFU`. Recover it **without**
the jump step:

```sh
python recover_flash.py /path/to/firmware.zip --retry 8
```

This connects directly to the bootloader's DFU service and re-sends the image. This has been validated
recovering exactly that state over BLE with no physical access.

## Expected results

| Run | PRN | high-mtu | typical result |
|-----|-----|----------|----------------|
| nRF Connect on iOS (reference) | 12→? | n/a | ~0.8–2.7 kB/s, ~1/5 succeed, silent stalls |
| `bench_flash.py` default | 8 | no | **~1.5 kB/s, first try, unattended** |

~1.5 kB/s is the legacy-DFU device floor at a 20-byte MTU — bench-proven identical on Windows and on
the Pi. A ~513 KB image takes ~6 minutes, unattended. That reliability (not speed) is the whole point.

## Notes / gotchas

- `start ota` brings up the app-side Bluefruit `bledfu` (advertises `RAK4631_OTA`); `bench_flash.py`'s
  buttonless jump reboots it into the Adafruit bootloader, which the tool then finds by DFU service
  UUID. **Do not double-tap reset for this flow** — it collides with the tool's jump step.
- Keep the board ~10–30 cm from the host's BLE antenna for a clean bench baseline.
- If `bench_flash.py` can't find `RAK4631_OTA`, the advert may have timed out — re-run Step 2.

---

**Disclaimer:** firmware updates carry risk. Ensure you have a recovery path (physical USB access, or
the SWD/J-Link interface) available before flashing nodes you can't easily reach. Use at your own risk.
