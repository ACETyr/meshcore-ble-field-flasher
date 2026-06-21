#!/usr/bin/env python3
"""BLE RSSI probe — measure link margin to a target before risking a multi-minute DFU.

Continuously scans and logs the target's advertised RSSI (timestamp, name, RSSI) + a rolling
min/mean/max. Cross-platform (bleak). At the mast: run at various distances/heights and find the
margin BEFORE attempting a flash. Rule of thumb for a stable ~5–11 min DFU: want the worst-case
RSSI comfortably above ~ -80 dBm (≈ >15 dB over the ~ -90 dBm BLE floor).

Usage:
  python3 ble_rssi_probe.py [NAME_SUBSTRING] [--seconds N]
  python3 ble_rssi_probe.py RAK4631_OTA
  python3 ble_rssi_probe.py 4631            # matches RAK4631_OTA and 4631_DFU
Tip: the node is only BLE-visible after `start ota` (RF-admin). For a bare-presence range check you
can also just point this at any always-on BLE beacon to sanity-check the adapter/antenna.
"""
import asyncio, argparse, time, statistics
from bleak import BleakScanner

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="RAK4631_OTA")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N s (0 = until Ctrl-C)")
    args = ap.parse_args()
    print(f"Probing RSSI for names containing '{args.target}'. Ctrl-C to stop.\n")
    samples = []

    def cb(device, adv):
        name = adv.local_name or device.name or ""
        if args.target.lower() in name.lower() or args.target.upper() == device.address.upper():
            samples.append(adv.rssi)
            print(f"{time.strftime('%H:%M:%S')}  {name:14.14} {device.address}  "
                  f"RSSI {adv.rssi:4d} dBm   (n={len(samples)} "
                  f"min={min(samples)} mean={statistics.mean(samples):.0f} max={max(samples)})")

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    t_end = (time.time() + args.seconds) if args.seconds else None
    try:
        while t_end is None or time.time() < t_end:
            await asyncio.sleep(0.5)
    finally:
        await scanner.stop()

    if samples:
        worst = min(samples)
        margin = worst + 90
        verdict = "COMFORTABLE — bare Pi likely fine" if margin >= 15 else \
                  "MARGINAL — consider Sena UD100-G03 + 18 dBi panel antenna"
        print(f"\nSummary: n={len(samples)} min={worst} mean={statistics.mean(samples):.0f} "
              f"max={max(samples)} dBm")
        print(f"Worst-case margin over -90 dBm floor: ~{margin} dB → {verdict}")
    else:
        print("\nNo matching device seen. Is it advertising? (need `start ota` on the node)")

if __name__ == "__main__":
    asyncio.run(main())
