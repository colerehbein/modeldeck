"""
core.py — shared, non-HTTP logic for Thrash (config, lanes, status).

Everything here is process-independent: no HTTP, no request handling, no
HTML. The HTTP server (thrash_server.py), the CLI, the doctor, the TTL
reaper and the tests all import this module so there is exactly one
implementation of lane control and status.

It merges two lane sources:
  1. ramtoggle --json   (launchd / LM Studio / router / ComfyUI lanes), if
     a ramtoggle path is configured
  2. the lane scripts declared in config/*.json (llama.cpp + uzu models)

Lane control:
  - script lanes  -> `<script> start|stop`, status via `<script> status`
  - ramtoggle lanes -> `ramtoggle on|off <id>`, status via `ramtoggle --json`

Status shape returned by get_status():
  { memory, lanes: [{id, label, kind, on, port, ram_mb, impact}], ts }

Config is loaded once at import time, mirroring the original
thrash_server.py behaviour: the globals below (CFG, SCRIPT_LANES,
RAMTOGGLE, ...) are the module's state and are patched directly by tests.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request  # noqa: F401  (used by the server's SSE proxy)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as config_mod

CFG = config_mod.load()
PORT = int(CFG["server"]["port"])
HOST = CFG["server"]["host"]
RAMTOGGLE = CFG.get("ramtoggle") or None
SCRIPT_LANES = {l["id"]: l for l in CFG.get("lanes", [])}
UZU_PROXY_LANE = CFG.get("uzu_proxy_lane")


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def script_lane_on(lane):
    """Ask the lane script for ground truth (on-demand servers may be down)."""
    rc, out, _ = run([lane["script"], "status"], timeout=15)
    return any(line.strip().startswith("UP") for line in out.splitlines())


def ramtoggle_status():
    """Return (lanes, memory) from ramtoggle --json, or ([], {}) if unavailable."""
    if not RAMTOGGLE:
        return [], {}
    rc, out, _ = run([RAMTOGGLE, "--json"], timeout=30)
    if rc != 0:
        return [], {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], {}
    lanes = [{
        "id": l["id"],
        "label": l["label"],
        "kind": l.get("kind", "ramtoggle"),
        "on": bool(l.get("on")),
        "port": l.get("port"),
        "ram_mb": l.get("approx_mb"),
        "impact": l.get("impact", ""),
    } for l in data.get("lanes", [])]
    return lanes, data.get("memory", {})


def get_status():
    rt_lanes, memory = ramtoggle_status()
    lanes = list(rt_lanes)
    for lane in CFG.get("lanes", []):
        lanes.append({
            "id": lane["id"],
            "label": lane["label"],
            "kind": lane.get("kind", "script"),
            "on": script_lane_on(lane),
            "port": lane.get("port"),
            "ram_mb": lane.get("approx_mb"),
            "impact": lane.get("impact", ""),
        })
    return {"memory": memory, "lanes": lanes, "ts": int(time.time())}


def toggle(lane_id, turn_on):
    if lane_id in SCRIPT_LANES:
        script = SCRIPT_LANES[lane_id]["script"]
        if turn_on:
            # start blocks while the model loads; spawn detached, then verify
            # the process actually launched (a failed exec is otherwise silent).
            try:
                proc = subprocess.Popen(
                    [script, "start"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                # missing / non-executable script — Popen raises before poll()
                return False, f"{lane_id} failed to start: {e}"
            time.sleep(0.5)
            if proc.poll() is not None:
                return False, (f"{lane_id} failed to start (exit {proc.returncode}) "
                               "— see the lane log")
            return True, f"{lane_id} starting (loads in background)"
        rc, out, err = run([script, "stop"], timeout=20)
        return rc == 0, (out.strip() or err.strip() or f"stop exited {rc}")
    if RAMTOGGLE:
        rt_lanes, _ = ramtoggle_status()
        if lane_id not in {l["id"] for l in rt_lanes}:
            return False, f"unknown lane: {lane_id}"
        action = "on" if turn_on else "off"
        rc, out, err = run([RAMTOGGLE, action, lane_id], timeout=60)
        return rc == 0, (out.strip() or err.strip() or f"ramtoggle exited {rc}")
    return False, f"unknown lane: {lane_id} (no ramtoggle configured)"


def uzu_proxy_port():
    if not UZU_PROXY_LANE:
        return None
    lane = SCRIPT_LANES.get(UZU_PROXY_LANE)
    return lane.get("port") if lane else None


def known_lane_ids():
    """Every lane id core can act on: script lanes ∪ ramtoggle lanes.

    Used by the CLI/doctor/reaper to validate an id before dispatching, so
    they do not each re-implement the two-source lookup.
    """
    ids = set(SCRIPT_LANES)
    for lane in ramtoggle_status()[0]:
        ids.add(lane["id"])
    return ids
