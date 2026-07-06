#!/usr/bin/env python3
"""MeshCore BLE field-flasher — web UI.

A phone-browser front end for flashing MeshCore nRF52 nodes over BLE — RAK4631 by default, or ANY
MeshCore board (*_OTA advertising name) via the "Target board" toggle. The flash runs SERVER-SIDE in this
process, decoupled from the browser connection -> a dropped/slept phone does NOT interrupt it; just
reload the page to see the live state. Reachable over the field AP (http://10.42.0.1), home WiFi, or
the USB-gadget link (http://10.55.0.1). Reuses the bench-proven nrf_dfu_py engine.

Manual buttons: Flash (start ota -> jump -> DFU), Recover (direct-to-bootloader 4631_DFU),
RSSI probe (range), Scan (list nearby BLE devices).

Drone mode: a web-armable, reboot-persistent auto-flash loop. While armed it scans for a node in OTA
mode and flashes it WITHOUT a human at the UI (for pole/drone deployment when ground->mast BLE range
is too short). RSSI-gated so it only flashes the strong, nearby node; auto-recovers a node left in
4631_DFU by a failed flash; auto-disarms after a configurable timeout. A failed flash is never a brick
(OTAFix auto-fallback), which is what makes unattended operation safe.

Firmware library: upload/select/delete DFU images over the web (no SSH/SCP). The active image is what
Flash and Drone mode use.

Run as root (port 80 + BLE + clock-set). One job at a time (manual ops and the auto-loop share _lock).
"""
import asyncio, threading, time, os, sys, json, shutil, subprocess, logging
from flask import Flask, jsonify, Response, redirect, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "nrf_dfu_py"))
from dfu_lib import (NordicLegacyDFU, find_device_by_name_or_address,
                     DfuException, DFU_SERVICE_UUID, UPLOAD_MODE_APPLICATION, _UPLOAD_MODE_NAMES)
from bleak import BleakScanner

FW_DIR = os.path.join(HERE, "firmware")
CONFIG_PATH = os.path.join(HERE, "config.json")
FLASHLOG_PATH = os.path.join(HERE, "flashlog.jsonl")
OTA_NAME = "RAK4631_OTA"
BL_NAME = "4631_DFU"
FLASH_TIMEOUT_S = 12 * 60          # hard ceiling per flash so a hung BLE stack can't freeze the loop
MAX_UPLOAD = 8 * 1024 * 1024       # firmware zips are ~0.5-1 MB; 8 MB is a generous cap

DEFAULT_CFG = {
    "active_firmware": "firmware.zip",
    "any_board": False,   # False = RAK4631 only (exact-name match); True = any MeshCore board (*_OTA)
    "autoflash": {"rssi_threshold_dbm": -80, "arm_timeout_min": 30, "cooldown_sec": 120},
    "runtime": {"armed": False},
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
_lock = threading.Lock()
JOB = {"status": "idle", "action": None, "pct": 0, "log": [], "result": None}
AUTO = {"armed": False, "state": "off", "since": 0.0, "deadline": 0.0,
        "flash_count": 0, "last": None, "thread": None}
_cooldowns = {}  # device address -> epoch when that address may be flashed again

def log(msg):
    JOB["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
    del JOB["log"][:-400]

# NOTE: dfu_lib emits each message via BOTH its python logger AND its log_callback. We use ONLY the
# callback (log_callback=log below) -- do NOT also attach a logging handler, or every line doubles.

def _progress(pct):
    JOB["pct"] = pct

# ----------------------------------------------------------------------------- config + firmware lib

def load_cfg():
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    cfg = json.loads(json.dumps(DEFAULT_CFG))  # deep copy of defaults
    if isinstance(raw, dict):
        if raw.get("active_firmware"):
            cfg["active_firmware"] = raw["active_firmware"]
        if isinstance(raw.get("any_board"), bool):
            cfg["any_board"] = raw["any_board"]
        if isinstance(raw.get("autoflash"), dict):
            cfg["autoflash"].update(raw["autoflash"])
        if isinstance(raw.get("runtime"), dict):
            cfg["runtime"].update(raw["runtime"])
    return cfg

def save_cfg(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)

def ensure_fw_dir():
    os.makedirs(FW_DIR, exist_ok=True)
    # migrate a legacy firmware.zip sitting next to the script into the library dir
    legacy = os.path.join(HERE, "firmware.zip")
    dest = os.path.join(FW_DIR, "firmware.zip")
    if os.path.exists(legacy) and not os.path.exists(dest):
        try:
            shutil.copy2(legacy, dest)
        except Exception:
            pass

def active_firmware_path():
    cfg = load_cfg()
    name = os.path.basename(cfg.get("active_firmware") or "firmware.zip")
    return os.path.join(FW_DIR, name)

def fw_type(path):
    """Parse a DFU zip's manifest and return its human firmware-type name, or 'INVALID'."""
    try:
        d = NordicLegacyDFU(path, 8, 0.4)
        d.parse_zip()
        return _UPLOAD_MODE_NAMES.get(d.upload_mode, f"0x{d.upload_mode:02x}")
    except Exception:
        return "INVALID"

def list_firmware():
    ensure_fw_dir()
    active = os.path.basename(load_cfg().get("active_firmware") or "")
    out = []
    for fn in sorted(os.listdir(FW_DIR)):
        if not fn.lower().endswith(".zip"):
            continue
        p = os.path.join(FW_DIR, fn)
        out.append({"name": fn, "size": os.path.getsize(p),
                    "type": fw_type(p), "active": fn == active})
    return out

# ------------------------------------------------------------------------------------ flash log

def _uptime():
    try:
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]), 1)
    except Exception:
        return 0.0

def flashlog_append(entry):
    rec = {"ts": int(time.time()), "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "ts_uptime": _uptime()}
    rec.update(entry)
    try:
        with open(FLASHLOG_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

def flashlog_tail(n=8):
    try:
        with open(FLASHLOG_PATH) as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []

# ------------------------------------------------------------------------------------ flash engine

def any_board():
    return bool(load_cfg().get("any_board"))

def _is_ota_name(name):
    """OTA-mode target match. RAK-only mode: exact RAK4631_OTA. Any-board mode: the *_OTA suffix,
    which every MeshCore nRF52 variant uses (T114_OTA, T1000E_OTA, TECHO_OTA, ...)."""
    if any_board():
        return name.upper().endswith("_OTA")
    return name == OTA_NAME

def _is_bootloader(name, adv=None):
    """Bootloader match. RAK-only mode: exact 4631_DFU. Any-board mode: any advertised DFU service
    (bootloaders always advertise it) or a DFU-ish name (4631_DFU, AdaDFU, ...)."""
    if not any_board():
        return name == BL_NAME
    uuids = [u.lower() for u in (adv.service_uuids or [])] if adv else []
    return DFU_SERVICE_UUID.lower() in uuids or "DFU" in name.upper()

def _ota_label():
    return "any *_OTA board" if any_board() else OTA_NAME

async def _find_ota_device():
    """Scan and return the strongest device currently in OTA mode (per the active board filter)."""
    found = await BleakScanner.discover(timeout=5.0, return_adv=True)
    best = None   # (rssi, dev, name)
    for _key, (d, adv) in found.items():
        name = adv.local_name or d.name or ""
        if _is_ota_name(name):
            rssi = adv.rssi if adv.rssi is not None else -999
            if best is None or rssi > best[0]:
                best = (rssi, d, name)
    if best is None:
        raise DfuException(f"no device in OTA mode found ({_ota_label()})")
    return best[1]

async def _find_bootloader(app_dev):
    try:
        bl = await find_device_by_name_or_address("DFU", force_scan=True, service_uuid=DFU_SERVICE_UUID)
        if bl:
            return bl
    except DfuException:
        pass
    mac = app_dev.address
    if ":" in mac and len(mac) == 17:
        hint = f"{mac[:-2]}{(int(mac[-2:], 16) + 1) & 0xFF:02X}"
        try:
            return await find_device_by_name_or_address(hint, force_scan=True)
        except DfuException:
            pass
    return None

async def do_flash(recover=False, address=None):
    """Flash the active firmware. `address` (a MAC string) targets a specific OTA node -- the drone loop
    passes the RSSI-gated MAC it chose, and we re-resolve a FRESH BLEDevice here so we never reuse a
    scanner/loop-bound device across event loops. Returns a result dict for the flash log."""
    fw = active_firmware_path()
    dfu = NordicLegacyDFU(fw, 8, 0.4, high_mtu=False, progress_callback=_progress, log_callback=log)
    dfu.parse_zip()
    n = len(dfu.bin_data)
    target_mac = None
    if recover:
        # BL_NAME matches the RAK bootloader exactly; the DFU service UUID fallback inside
        # find_device_by_name_or_address catches every other board's bootloader.
        log(f"Recovery: scanning for bootloader ({BL_NAME} or any DFU service) ...")
        bl = await find_device_by_name_or_address(BL_NAME, force_scan=True, service_uuid=DFU_SERVICE_UUID)
    else:
        if address is not None:
            log(f"Re-acquiring {address} ...")
            dev = await find_device_by_name_or_address(address, force_scan=True)
        else:
            log(f"Scanning for {_ota_label()} (issue `start ota` on the node first) ...")
            dev = await _find_ota_device()
        target_mac = dev.address
        log(f"Found {dev.name} ({dev.address}) — jumping to bootloader ...")
        await dfu.jump_to_bootloader(dev)
        await asyncio.sleep(5.0)
        bl = await _find_bootloader(dev)
    if not bl:
        raise DfuException("DFU bootloader not found")
    if target_mac is None:
        target_mac = bl.address
    log(f"Bootloader {bl.address} — uploading {n} bytes ...")
    t0 = time.perf_counter()
    await dfu.perform_update(bl, max_retries=8)
    dt = time.perf_counter() - t0
    kbps = round(n / 1024 / dt, 2)
    JOB["result"] = f"SUCCESS — {n} bytes in {dt:.0f}s = {kbps:.2f} kB/s"
    JOB["pct"] = 100
    return {"target_mac": target_mac, "bytes": n, "kbps": kbps, "firmware": os.path.basename(fw)}

async def do_flash_logged(recover=False, mode="flash"):
    """Manual flash/recover wrapper: records both outcomes to the persistent flash log."""
    try:
        info = await do_flash(recover=recover)
        flashlog_append({"mode": mode, "result": "SUCCESS", **info})
    except Exception as e:
        flashlog_append({"mode": mode, "result": f"FAILED: {e}"})
        raise

async def do_rssi(seconds=30):
    samples = []
    def cb(d, adv):
        name = adv.local_name or d.name or ""
        if _is_ota_name(name) or _is_bootloader(name, adv) or (not any_board() and "4631" in name):
            samples.append(adv.rssi)
            log(f"{name} {d.address}  RSSI {adv.rssi} dBm  "
                f"(min {min(samples)} mean {sum(samples)//len(samples)} max {max(samples)})")
    sc = BleakScanner(detection_callback=cb)
    await sc.start()
    t_end = time.time() + seconds
    while time.time() < t_end:
        JOB["pct"] = min(99, int((seconds - (t_end - time.time())) / seconds * 100))
        await asyncio.sleep(0.5)
    await sc.stop()
    if samples:
        worst = min(samples)
        verdict = "COMFORTABLE" if worst + 90 >= 15 else "MARGINAL (needs Sena/panel antenna)"
        JOB["result"] = (f"RSSI min {worst} / mean {sum(samples)//len(samples)} / max {max(samples)} dBm "
                         f"— margin ~{worst+90} dB → {verdict}")
    else:
        JOB["result"] = f"no device seen ({_ota_label()}) — issue `start ota` on the node first"
    JOB["pct"] = 100

async def do_scan(seconds=6):
    log(f"Scanning {seconds}s for BLE devices ...")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for _key, (d, adv) in found.items():
        rows.append((adv.rssi if adv.rssi is not None else -999,
                     adv.local_name or d.name or "?", d.address, adv))
    rows.sort(reverse=True, key=lambda r: r[0])  # strongest first
    if not rows:
        log("no BLE devices seen")
    for rssi, name, addr, adv in rows:
        mark = "  <-- OTA" if _is_ota_name(name) else ("  <-- bootloader" if _is_bootloader(name, adv) else "")
        log(f"{rssi:>4} dBm  {name:<20} {addr}{mark}")
    JOB["result"] = f"{len(rows)} device(s) seen — strongest first (see log)"
    JOB["pct"] = 100

def run_job(action, factory):
    with _lock:
        if JOB["status"] == "running":
            return False
        JOB.update(status="running", action=action, pct=0, log=[], result=None)
    def worker():
        try:
            asyncio.run(factory())
            JOB["status"] = "done"
        except Exception as e:
            JOB["result"] = f"FAILED: {e}"
            log("ERROR: " + str(e))
            JOB["status"] = "error"
    threading.Thread(target=worker, daemon=True).start()
    return True

# ----------------------------------------------------------------------------------- drone mode

async def _scan_candidate(threshold):
    """One BLE scan -> a flash target. Prefer recovering a node stuck in the bootloader (currently
    broken), else the STRONGEST OTA-mode node (per the board filter) at/above the RSSI threshold."""
    found = await BleakScanner.discover(timeout=5.0, return_adv=True)
    best_ota = None       # (rssi, dev)
    recover_dev = None
    for _key, (d, adv) in found.items():
        name = adv.local_name or d.name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if name == BL_NAME or DFU_SERVICE_UUID.lower() in uuids:
            recover_dev = d
        elif _is_ota_name(name) and adv.rssi is not None and adv.rssi >= threshold:
            if best_ota is None or adv.rssi > best_ota[0]:
                best_ota = (adv.rssi, d)
    if recover_dev is not None:
        return ("recover", recover_dev)
    if best_ota is not None:
        return ("ota", best_ota[1])
    return None

def autoflash_loop():
    log("[drone] loop started")
    try:
        while AUTO["armed"]:
            if AUTO["deadline"] and time.time() >= AUTO["deadline"]:
                log("[drone] auto-disarm: arm timeout reached")
                break
            cfg = load_cfg()
            threshold = cfg["autoflash"]["rssi_threshold_dbm"]
            cooldown = cfg["autoflash"]["cooldown_sec"]
            try:
                cand = asyncio.run(_scan_candidate(threshold))
            except Exception as e:
                log(f"[drone] scan error: {e}")
                cand = None
            if cand is None:
                AUTO["state"] = "armed"
                time.sleep(2.0)
                continue
            kind, dev = cand
            mac = dev.address
            if _cooldowns.get(mac, 0) > time.time():
                AUTO["state"] = "armed"
                time.sleep(2.0)
                continue
            if not _lock.acquire(blocking=False):   # a manual op holds the lock -> wait, retry
                time.sleep(1.0)
                continue
            mode = "auto-recover" if kind == "recover" else "auto"
            try:
                JOB.update(status="running", action=mode, pct=0, log=[], result=None)
                AUTO["state"] = "flashing"
                log(f"[drone] target {mac} ({kind}) — flashing active firmware")
                recover = (kind == "recover")
                info = asyncio.run(asyncio.wait_for(
                    do_flash(recover=recover, address=(None if recover else mac)),
                    timeout=FLASH_TIMEOUT_S))
                JOB["status"] = "done"
                flashlog_append({"mode": mode, "result": "SUCCESS", **info})
                AUTO["flash_count"] += 1
                AUTO["last"] = {"mac": mac, "result": "SUCCESS", "kbps": info.get("kbps")}
                AUTO["state"] = "ok"
                _cooldowns[mac] = time.time() + cooldown   # don't re-flash the same node for a while
            except Exception as e:
                JOB["result"] = f"FAILED: {e}"
                JOB["status"] = "error"
                log(f"[drone] flash failed: {e}")
                flashlog_append({"mode": mode, "result": f"FAILED: {e}", "target_mac": mac})
                AUTO["last"] = {"mac": mac, "result": f"FAILED: {e}"}
                AUTO["state"] = "fail"
                _cooldowns[mac] = time.time() + 15         # short backoff, then auto-retry/recover
            finally:
                _lock.release()
            time.sleep(2.0)
    finally:
        AUTO["armed"] = False
        AUTO["state"] = "off"
        AUTO["thread"] = None
        try:
            cfg = load_cfg(); cfg["runtime"]["armed"] = False; save_cfg(cfg)
        except Exception:
            pass
        log("[drone] loop stopped")

def start_autoflash():
    cfg = load_cfg()
    AUTO["armed"] = True
    AUTO["since"] = time.time()
    tm = cfg["autoflash"]["arm_timeout_min"]
    AUTO["deadline"] = (time.time() + tm * 60) if tm else 0.0
    AUTO["state"] = "armed"
    if AUTO.get("thread") is None or not AUTO["thread"].is_alive():
        t = threading.Thread(target=autoflash_loop, daemon=True)
        AUTO["thread"] = t
        t.start()

# ----------------------------------------------------------------------------------------- routes

@app.post("/flash")
def r_flash():
    return (jsonify(ok=True) if run_job("flash", lambda: do_flash_logged(False, "flash"))
            else (jsonify(error="busy"), 409))

@app.post("/recover")
def r_recover():
    return (jsonify(ok=True) if run_job("recover", lambda: do_flash_logged(True, "recover"))
            else (jsonify(error="busy"), 409))

@app.post("/rssi")
def r_rssi():
    return (jsonify(ok=True) if run_job("rssi", lambda: do_rssi(30)) else (jsonify(error="busy"), 409))

@app.post("/scan")
def r_scan():
    return (jsonify(ok=True) if run_job("scan", lambda: do_scan(6)) else (jsonify(error="busy"), 409))

@app.post("/drone/on")
def r_drone_on():
    cfg = load_cfg(); cfg["runtime"]["armed"] = True; save_cfg(cfg)
    start_autoflash()
    log("[drone] ARMED")
    return jsonify(ok=True)

@app.post("/drone/off")
def r_drone_off():
    AUTO["armed"] = False                      # loop exits after the current (if any) flash finishes
    cfg = load_cfg(); cfg["runtime"]["armed"] = False; save_cfg(cfg)
    log("[drone] DISARMED")
    return jsonify(ok=True)

@app.post("/settings")
def r_settings():
    data = request.get_json(silent=True) or request.form
    cfg = load_cfg()
    af = cfg["autoflash"]
    def _clamp(key, lo, hi):
        v = data.get(key)
        if v in (None, ""):
            return
        try:
            af[key] = max(lo, min(hi, int(v)))
        except Exception:
            pass
    _clamp("rssi_threshold_dbm", -120, 0)
    _clamp("arm_timeout_min", 0, 1440)
    _clamp("cooldown_sec", 0, 3600)
    if "any_board" in data:
        cfg["any_board"] = str(data.get("any_board")).lower() in ("1", "true", "on", "yes")
        log(f"board filter: {'ANY board (*_OTA)' if cfg['any_board'] else 'RAK4631 only'}")
    save_cfg(cfg)
    if AUTO["armed"]:                          # apply a new timeout to the running arm immediately
        tm = af["arm_timeout_min"]
        AUTO["deadline"] = (AUTO["since"] + tm * 60) if tm else 0.0
    return jsonify(ok=True, autoflash=af, any_board=cfg["any_board"])

@app.get("/firmware")
def r_fw_list():
    return jsonify(firmware=list_firmware())

@app.post("/upload")
def r_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file"), 400
    name = os.path.basename(f.filename)
    if not name.lower().endswith(".zip"):
        return jsonify(error="must be a .zip DFU package"), 400
    ensure_fw_dir()
    dest = os.path.join(FW_DIR, name)
    tmp = dest + ".part"
    f.save(tmp)
    try:
        d = NordicLegacyDFU(tmp, 8, 0.4)
        d.parse_zip()                          # raises on anything that isn't a valid DFU package
        mode = d.upload_mode
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return jsonify(error=f"invalid DFU zip: {e}"), 400
    os.replace(tmp, dest)
    mode_name = _UPLOAD_MODE_NAMES.get(mode, f"0x{mode:02x}")
    app_only = (mode == UPLOAD_MODE_APPLICATION)
    activate = request.form.get("activate", "1") != "0"
    if activate:
        cfg = load_cfg(); cfg["active_firmware"] = name; save_cfg(cfg)
    log(f"uploaded {name} ({mode_name})" + ("" if app_only else "  WARNING: not app-only"))
    return jsonify(ok=True, name=name, type=mode_name, app_only=app_only, active=activate)

@app.post("/firmware/select")
def r_fw_select():
    data = request.get_json(silent=True) or request.form
    name = os.path.basename(data.get("name", ""))
    if not os.path.exists(os.path.join(FW_DIR, name)):
        return jsonify(error="not found"), 404
    cfg = load_cfg(); cfg["active_firmware"] = name; save_cfg(cfg)
    return jsonify(ok=True, active=name)

@app.post("/firmware/delete")
def r_fw_delete():
    data = request.get_json(silent=True) or request.form
    name = os.path.basename(data.get("name", ""))
    if name == os.path.basename(load_cfg().get("active_firmware") or ""):
        return jsonify(error="cannot delete the active image"), 400
    p = os.path.join(FW_DIR, name)
    if os.path.exists(p):
        os.remove(p)
    return jsonify(ok=True)

@app.get("/firmware/<path:name>")
def r_fw_download(name):
    p = os.path.join(FW_DIR, os.path.basename(name))
    if not os.path.exists(p):
        return jsonify(error="not found"), 404
    return send_file(p, as_attachment=True)

@app.get("/flashlog")
def r_flashlog():
    if not os.path.exists(FLASHLOG_PATH):
        return jsonify(error="no log yet"), 404
    return send_file(FLASHLOG_PATH, as_attachment=True)

@app.post("/clock")
def r_clock():
    # The Pi has no RTC and no NTP in the field -> let the phone seed the clock so flash-log timestamps
    # are real. Only set it when ours is obviously unset (year < 2025); never fight a sane/NTP clock.
    data = request.get_json(silent=True) or request.form
    iso = data.get("iso")
    if not iso:
        return jsonify(error="no time"), 400
    if time.gmtime().tm_year >= 2025:
        return jsonify(ok=True, skipped="clock already sane")
    try:
        subprocess.run(["date", "-u", "-s", iso], check=True, capture_output=True)
        return jsonify(ok=True, set=iso)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/status")
def r_status():
    cfg = load_cfg()
    fw = active_firmware_path()
    drone = {"armed": AUTO["armed"], "state": AUTO["state"], "flash_count": AUTO["flash_count"],
             "last": AUTO["last"],
             "expires_in": (max(0, int(AUTO["deadline"] - time.time()))
                            if (AUTO["armed"] and AUTO["deadline"]) else None)}
    return jsonify(status=JOB["status"], action=JOB["action"], pct=JOB["pct"],
                   result=JOB["result"], log=JOB["log"][-80:],
                   firmware=os.path.basename(fw), firmware_ok=os.path.exists(fw),
                   autoflash=cfg["autoflash"], any_board=cfg["any_board"],
                   drone=drone, history=flashlog_tail(8))

@app.get("/")
def r_index():
    return Response(INDEX_HTML, mimetype="text/html")

# Captive portal: phones probe a connectivity-check URL on join (Apple captive.apple.com/hotspot-detect,
# Android /generate_204, Windows /ncsi.txt, ...). Combined with the AP's wildcard DNS (all hosts -> the Pi),
# every such probe lands here; a 302 to "/" makes the phone auto-open the flasher page. Real API routes
# above are more specific and still match first.
@app.route("/<path:_path>")
def captive(_path):
    return redirect("/", code=302)

INDEX_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>MeshCore Field Flasher</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#0e1116;color:#e6edf3}
.wrap{max-width:560px;margin:0 auto;padding:16px}
h1{font-size:1.15rem;margin:.2rem 0 .8rem}
h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.04em;color:#8b949e;margin:1.1rem 0 .3rem}
.badge{display:inline-block;padding:.15rem .6rem;border-radius:1rem;font-size:.8rem;font-weight:600}
.idle{background:#30363d}.running{background:#9e6a03}.done{background:#1a7f37}.error{background:#b62324}
button{width:100%;padding:1rem;margin:.4rem 0;font-size:1.1rem;font-weight:600;border:0;border-radius:.6rem;color:#fff}
.flash{background:#1f6feb}.recover{background:#8957e5}.rssi{background:#1a7f37}.scan{background:#30475e}
button:disabled{opacity:.45}
.bar{height:14px;background:#21262d;border-radius:7px;overflow:hidden;margin:.6rem 0}
.bar>div{height:100%;width:0;background:#1f6feb;transition:width .3s}
.res{margin:.6rem 0;padding:.6rem;border-radius:.5rem;background:#161b22;font-weight:600;min-height:1.2rem}
pre{background:#161b22;padding:.6rem;border-radius:.5rem;height:34vh;overflow:auto;font-size:.78rem;white-space:pre-wrap;margin:.4rem 0}
.fw{font-size:.8rem;color:#8b949e}
.card{background:#161b22;border:1px solid #21262d;border-radius:.6rem;padding:.7rem .8rem;margin:.4rem 0}
.drone{display:flex;align-items:center;justify-content:space-between;gap:.6rem}
.switch{position:relative;display:inline-block;width:62px;height:34px;flex:0 0 auto}
.switch input{display:none}
.track{position:absolute;inset:0;border-radius:34px;background:#30363d;transition:.2s;cursor:pointer}
.track:before{content:"";position:absolute;height:26px;width:26px;left:4px;top:4px;border-radius:50%;background:#fff;transition:.2s}
input:checked+.track:before{transform:translateX(28px)}
#anySw:checked+.track{background:#1f6feb}
.d-off .track{background:#30363d}.d-armed .track{background:#9e6a03}.d-flashing .track{background:#1f6feb}
.d-ok .track{background:#1a7f37}.d-fail .track{background:#b62324}
.dinfo{font-size:.82rem;color:#cdd9e5}
.row{display:flex;align-items:center;gap:.5rem;font-size:.85rem;margin:.25rem 0}
.row label{flex:1;color:#8b949e}.row input[type=number]{width:90px;background:#0e1116;color:#e6edf3;border:1px solid #30363d;border-radius:.4rem;padding:.35rem}
.mini{padding:.45rem .7rem;font-size:.85rem;width:auto;margin:0}
.muted{color:#8b949e;font-size:.78rem}
.fwitem{display:flex;align-items:center;gap:.5rem;font-size:.85rem;margin:.2rem 0}
.fwitem .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fwitem .ty{color:#8b949e;font-size:.74rem}
details summary{cursor:pointer;color:#8b949e;font-size:.85rem;margin:.3rem 0}
table.hist{width:100%;border-collapse:collapse;font-size:.74rem}
table.hist td{padding:.2rem .3rem;border-top:1px solid #21262d;vertical-align:top}
.ok{color:#3fb950}.bad{color:#f85149}
.actbar{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.actbar label{color:#8b949e;font-size:.8rem}
select{background:#0e1116;color:#e6edf3;border:1px solid #30363d;border-radius:.4rem;padding:.45rem;font-size:.95rem;flex:1;min-width:150px}
.actype{font-size:.78rem;color:#8b949e;width:100%}
.dflash{font-size:.82rem;color:#cdd9e5;margin:.5rem 0 .1rem}
.fwscroll{max-height:150px;overflow:auto}
</style></head><body><div class=wrap>
<h1>MeshCore Field Flasher &nbsp;<span id=badge class="badge idle">idle</span></h1>

<h2>Active firmware</h2>
<div class="card actbar">
 <label for=fwsel>flashes</label>
 <select id=fwsel onchange="selectFw(this.value)"></select>
 <div class=actype id=acttype></div>
</div>

<h2>Target board</h2>
<div class="card drone">
 <div><div style="font-weight:600" id=anyLbl>RAK4631 only</div>
  <div class=dinfo>off = RAK4631_OTA only &middot; on = any MeshCore board (*_OTA)</div></div>
 <label class=switch><input type=checkbox id=anySw onchange="anyToggle(this.checked)">
  <span class=track></span></label>
</div>

<h2>Drone mode (unattended auto-flash)</h2>
<div class="card" id=droneCard>
 <div class=drone>
  <div><div style="font-weight:600">Drone mode</div><div class=dinfo id=dinfo>off</div></div>
  <label class=switch><input type=checkbox id=droneSw onchange="droneToggle(this.checked)">
   <span class=track></span></label>
 </div>
 <div class=dflash>Auto-flashes: <b id=droneFw>—</b></div>
 <details><summary>auto-flash settings</summary>
  <div class=row><label>RSSI threshold (dBm)</label><input type=number id=sRssi step=1></div>
  <div class=row><label>arm timeout (min, 0=∞)</label><input type=number id=sTimeout min=0 step=1></div>
  <div class=row><label>per-node cooldown (s)</label><input type=number id=sCooldown min=0 step=1></div>
  <button class="mini scan" style="width:100%" onclick="saveSettings()">Save settings</button>
  <div class=muted style="margin-top:.4rem">Armed state survives a reboot (timeout restarts). Disarm any
   time with the switch — an in-flight flash finishes first.</div>
 </details>
</div>

<h2>Manual</h2>
<button class=flash id=bFlash onclick="act('flash')">Flash active image</button>
<button class=rssi id=bRssi onclick="act('rssi')">RSSI / range probe</button>
<button class=scan id=bScan onclick="act('scan')">Scan (what do I see?)</button>
<button class=recover id=bRecover onclick="act('recover')">Recover (bootloader)</button>
<div class=bar><div id=bar></div></div>
<div class=res id=res></div>
<pre id=log></pre>

<h2>Firmware library <span class=muted style="text-transform:none;letter-spacing:0">— manage; active is set above</span></h2>
<div class="card fwscroll" id=fwlist><div class=muted>loading…</div></div>
<div class=row>
 <input type=file id=fwfile accept=".zip" style="flex:1;font-size:.8rem">
 <button class="mini flash" onclick="upload()">Upload</button>
</div>
<div class=muted id=upmsg></div>

<details><summary>flash history</summary>
 <table class=hist id=hist></table>
 <div class=muted><a href="/flashlog" style="color:#58a6ff">download full log</a></div>
</details>

</div><script>
let clockSent=false;
async function act(a){await fetch('/'+a,{method:'POST'});poll();}
async function droneToggle(on){
 if(on){
  const fw=(document.getElementById('droneFw').textContent||'(active image)');
  const tgt=document.getElementById('anySw').checked?'ANY MeshCore board (*_OTA)':'any RAK4631';
  if(!confirm('Arm Drone mode?\\n\\nIt will AUTO-FLASH this image to '+tgt+' in OTA mode above the '
   +'RSSI threshold:\\n\\n  '+fw+'\\n\\nCheck the active image is correct before arming.')){
   document.getElementById('droneSw').checked=false;return;}
 }
 await fetch('/drone/'+(on?'on':'off'),{method:'POST'});poll();
}
async function anyToggle(on){
 await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({any_board:on})});poll();
}
async function saveSettings(){
 const b={rssi_threshold_dbm:sRssi.value,arm_timeout_min:sTimeout.value,cooldown_sec:sCooldown.value};
 await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 poll();
}
function setVal(id,v){const e=document.getElementById(id);if(document.activeElement!==e)e.value=v;}
async function loadFw(){
 try{const j=await(await fetch('/firmware')).json();const box=document.getElementById('fwlist');
  const sel=document.getElementById('fwsel');const active=(j.firmware.find(f=>f.active)||{});
  // always-visible active selector (constant height regardless of image count)
  if(document.activeElement!==sel)
   sel.innerHTML=j.firmware.length
    ?j.firmware.map(f=>`<option value="${f.name}" ${f.active?'selected':''}>${f.name}</option>`).join('')
    :'<option>(no images)</option>';
  const at=document.getElementById('acttype');
  at.textContent=active.name?(active.type+(active.type!=='Application'?' ⚠ not app-only':'')):'no active image';
  at.className='actype'+(active.type&&active.type!=='Application'?' bad':'');
  // management list (scroll-contained so many images can't push the page)
  if(!j.firmware.length){box.innerHTML='<div class=muted>no images — upload one below</div>';return;}
  box.innerHTML=j.firmware.map(f=>{
   const kb=(f.size/1024).toFixed(0);
   const warn=(f.type!=='Application')?' ⚠':'';
   return `<div class=fwitem>
    <input type=radio name=fw ${f.active?'checked':''} onchange="selectFw('${f.name}')">
    <span class=nm>${f.name}</span>
    <span class=ty>${f.type}${warn} · ${kb} KB</span>
    ${f.active?'':`<button class="mini recover" onclick="delFw('${f.name}')">✕</button>`}
   </div>`;}).join('');
 }catch(e){}
}
async function selectFw(n){await fetch('/firmware/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});loadFw();poll();}
async function delFw(n){await fetch('/firmware/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});loadFw();}
async function upload(){
 const f=document.getElementById('fwfile').files[0];const m=document.getElementById('upmsg');
 if(!f){m.textContent='pick a .zip first';return;}
 m.textContent='uploading '+f.name+' …';
 const fd=new FormData();fd.append('file',f);
 try{const r=await fetch('/upload',{method:'POST',body:fd});const j=await r.json();
  m.textContent=j.ok?('uploaded '+j.name+' ('+j.type+(j.app_only?'':' — WARNING: not app-only')+')'):('error: '+j.error);
 }catch(e){m.textContent='upload failed';}
 loadFw();poll();
}
function histRow(h){
 const ok=(''+h.result).startsWith('SUCCESS');
 const cls=ok?'ok':'bad';const extra=h.kbps?(' '+h.kbps+' kB/s'):'';
 return `<tr><td>${h.ts_iso||''}</td><td>${h.mode||''}</td><td>${h.target_mac||''}</td>
  <td class=${cls}>${ok?'OK'+extra:h.result}</td></tr>`;
}
async function poll(){
 try{const s=await(await fetch('/status')).json();
  const b=document.getElementById('badge');b.textContent=s.status;b.className='badge '+s.status;
  document.getElementById('bar').style.width=(s.pct||0)+'%';
  document.getElementById('res').textContent=s.result||'';
  document.getElementById('log').textContent=(s.log||[]).join('\\n');
  document.getElementById('log').scrollTop=1e9;
  document.getElementById('droneFw').textContent=s.firmware+(s.firmware_ok?'':' (MISSING!)');
  const run=s.status==='running';
  for(const id of ['bFlash','bRssi','bRecover','bScan'])document.getElementById(id).disabled=run;
  // drone
  const d=s.drone||{};const sw=document.getElementById('droneSw');sw.checked=!!d.armed;
  const card=document.getElementById('droneCard');card.className='card d-'+(d.state||'off');
  let info=d.armed?'ARMED':'off';
  if(d.armed){info='ARMED · scanning';if(d.state==='flashing')info='FLASHING';
   if(d.expires_in!=null)info+=' · auto-off in '+Math.ceil(d.expires_in/60)+'m';
   info+=' · '+(d.flash_count||0)+' flashed';}
  if(d.last)info+=' · last '+d.last.mac+' '+(('' +d.last.result).startsWith('SUCCESS')?'OK':'FAIL');
  document.getElementById('dinfo').textContent=info;
  // target board toggle
  const asw=document.getElementById('anySw');
  if(document.activeElement!==asw)asw.checked=!!s.any_board;
  document.getElementById('anyLbl').textContent=s.any_board?'Any board (*_OTA)':'RAK4631 only';
  // settings (don't clobber while typing)
  if(s.autoflash){setVal('sRssi',s.autoflash.rssi_threshold_dbm);
   setVal('sTimeout',s.autoflash.arm_timeout_min);setVal('sCooldown',s.autoflash.cooldown_sec);}
  // history
  document.getElementById('hist').innerHTML=(s.history||[]).slice().reverse().map(histRow).join('');
  // seed the clock once from the phone (no RTC/NTP in the field)
  if(!clockSent){clockSent=true;fetch('/clock',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({iso:new Date().toISOString()})}).catch(()=>{});}
 }catch(e){}
}
loadFw();setInterval(poll,1000);poll();
</script></body></html>"""

if __name__ == "__main__":
    ensure_fw_dir()
    if load_cfg().get("runtime", {}).get("armed"):
        log("[drone] resuming armed state after start (timeout restarted)")
        start_autoflash()
    app.run(host="0.0.0.0", port=80, threaded=True)
