#!/usr/bin/env python3
"""Direct-to-bootloader flash (NO jump) — recovery path when the device is already in DFU mode,
e.g. after a failed/aborted flash left it in the OTAFix bootloader advertising as `4631_DFU`
(DFU service 00001530-...). Uses the known-good default settings (PRN=8, no high-MTU).

Usage:
  python recover_flash.py <zip> [--name 4631_DFU] [--prn 8] [--delay 0.4]
"""
import asyncio, argparse, logging, sys, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "nrf_dfu_py"))
from dfu_lib import (NordicLegacyDFU, find_device_by_name_or_address,
                     DfuException, DFU_SERVICE_UUID)

def progress(pct):
    sys.stdout.write(f"\rUploading: {pct}%"); sys.stdout.flush()
    if pct == 100: sys.stdout.write("\n")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--name", default="4631_DFU")
    ap.add_argument("--prn", type=int, default=8)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--retry", type=int, default=5)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bleak").setLevel(logging.WARNING)

    dfu = NordicLegacyDFU(args.file, args.prn, args.delay, high_mtu=False, progress_callback=progress)
    dfu.parse_zip()
    print(f"Firmware: {len(dfu.bin_data)} bytes, mode=0x{dfu.upload_mode:02x}")

    print(f"Scanning for bootloader {args.name} (DFU service)...")
    bl = await find_device_by_name_or_address(args.name, force_scan=True, service_uuid=DFU_SERVICE_UUID)
    print(f"Found {bl.name} ({bl.address})")

    t0 = time.perf_counter()
    await dfu.perform_update(bl, max_retries=args.retry)
    dt = time.perf_counter() - t0
    print(f"\n=== RECOVERY SUCCESS — {len(dfu.bin_data)} bytes in {dt:.1f}s "
          f"= {len(dfu.bin_data)/1024/dt:.2f} kB/s ===")

if __name__ == "__main__":
    asyncio.run(main())
