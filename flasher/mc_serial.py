#!/usr/bin/env python3
"""Minimal MeshCore repeater serial-CLI helper (RAK4631 text CLI over USB CDC).

Proven params from reference/stage3_test.py: 115200 baud, line ending '\\r', NO login needed
(commands typed directly on the local USB console).

Usage:
  python mc_serial.py <PORT> "<command>" [read_seconds]
Examples:
  python mc_serial.py COM12 "ver" 1.5
  python mc_serial.py COM12 "get public.key" 1.5
  python mc_serial.py COM12 "start ota" 3      # -> device advertises RAK4631_OTA, prints OK - mac: ..
  python mc_serial.py COM12 "" 5               # just listen 5s (e.g. watch for RAW: debug logging)
"""
import sys, time, serial

def main():
    port = sys.argv[1]
    cmd = sys.argv[2] if len(sys.argv) > 2 else ""
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0

    with serial.Serial(port, 115200, timeout=0.3) as ser:
        time.sleep(0.3)
        ser.reset_input_buffer()
        if cmd:
            ser.write((cmd + "\r").encode())
            ser.flush()
        end = time.time() + secs
        buf = b""
        while time.time() < end:
            buf += ser.read(ser.in_waiting or 1)
            time.sleep(0.03)
        sys.stdout.write(buf.decode("utf-8", "replace"))
        sys.stdout.write("\n")

if __name__ == "__main__":
    main()
