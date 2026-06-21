#!/usr/bin/env python3
"""Bench-flash a RAK4631 over BLE via nrf_dfu_py (Windows onboard BLE) and MEASURE it.

Mirrors dfu_cli.py's orchestration (jump -> bootloader -> update) but wraps the update in a
perf timer so we get an end-to-end flash time + effective kB/s -- the number to compare against
the iPhone's flaky 0.8-2.7 kB/s. Prereq: the device must already be advertising as RAK4631_OTA,
i.e. issue `start ota` over serial first (see RUNBOOK.md).

Usage:
  python bench_flash.py <zip> [--name RAK4631_OTA] [--prn 8] [--delay 0.4] [--high-mtu]
                              [--retry 5] [--verbose]
"""
import asyncio, argparse, logging, sys, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "nrf_dfu_py"))
from dfu_lib import (NordicLegacyDFU, find_any_device, find_device_by_name_or_address,
                     DfuException, DFU_SERVICE_UUID)

def progress(pct):
    sys.stdout.write(f"\rUploading: {pct}%")
    sys.stdout.flush()
    if pct == 100:
        sys.stdout.write("\n")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--name", default="RAK4631_OTA")
    ap.add_argument("--prn", type=int, default=8)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--high-mtu", action="store_true")
    ap.add_argument("--retry", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("DFU_LIB").setLevel(logging.DEBUG if args.verbose else logging.INFO)

    dfu = NordicLegacyDFU(args.file, args.prn, args.delay, high_mtu=args.high_mtu,
                          progress_callback=progress)
    dfu.parse_zip()
    app_bytes = len(dfu.bin_data)
    print(f"Firmware: {args.file}  ({app_bytes} bytes, mode=0x{dfu.upload_mode:02x}, "
          f"prn={args.prn}, high_mtu={args.high_mtu})")

    print(f"Scanning for {args.name} ...")
    try:
        app_device = await find_any_device([args.name])
    except DfuException:
        print(f"ERROR: {args.name} not advertising. Issue `start ota` over serial first.")
        sys.exit(1)
    print(f"Found {app_device.name} ({app_device.address})")

    await dfu.jump_to_bootloader(app_device)
    print("Waiting for reboot (5s)...")
    await asyncio.sleep(5.0)

    bl = None
    try:
        bl = await find_device_by_name_or_address("DFU", force_scan=True,
                                                  service_uuid=DFU_SERVICE_UUID)
    except DfuException:
        pass
    if not bl:                          # MAC+1 fallback, same as dfu_cli.py
        mac = app_device.address
        if ":" in mac and len(mac) == 17:
            hint = f"{mac[:-2]}{(int(mac[-2:], 16) + 1) & 0xFF:02X}"
            try:
                bl = await find_device_by_name_or_address(hint, force_scan=True)
            except DfuException:
                pass
    if not bl:
        print("ERROR: DFU bootloader not found after jump.")
        sys.exit(1)

    t0 = time.perf_counter()
    await dfu.perform_update(bl, max_retries=args.retry)
    dt = time.perf_counter() - t0
    kbps = app_bytes / 1024 / dt if dt > 0 else 0
    print(f"\n=== RESULT: SUCCESS — {app_bytes} bytes flashed in {dt:.1f}s "
          f"= {kbps:.2f} kB/s  (PRN={args.prn}, high_mtu={args.high_mtu}) ===")
    print("    (end-to-end: stream + flash-write-verify + activate; comparable to the iPhone time)")

if __name__ == "__main__":
    asyncio.run(main())
