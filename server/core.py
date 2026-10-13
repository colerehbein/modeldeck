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


def ram_guard_mode():
    """RAM guard policy: "warn" (start anyway, flagged) or "block" (refuse).

    Defaults to "warn" so a config written before this block existed keeps
    the old always-start behaviour.
    """
    guard = CFG.get("ram_guard") or {}
    mode = guard.get("mode")
    return mode if mode in ("warn", "block") else "warn"


def project_start(lane):
    """Project the memory state after starting `lane`.

    Returns (projected_free_mb, would_thrash, total_mb). projected is None
    when no ramtoggle is available (no free_mb to project from) — in that
    case there is nothing to guard and would_thrash is False, so callers
    must never block on a None projection.
    """
    memory = get_status()["memory"]
    free = memory.get("free_mb")
    total = memory.get("total_mb")
    cost = lane.get("approx_mb") or 0
    if free is None:
        return None, False, total
    projected = free - cost
    would_thrash = projected < 1500 or (total and projected / total < 0.10)
    return projected, would_thrash, total


# A log file larger than this is read by seeking to the tail instead of
# slurping the whole thing, so a multi-GB lane log cannot blow up memory.
_LOG_TAIL_READ_THRESHOLD = 5 * 1024 * 1024
# Hard ceiling on bytes the seek path will read, independent of line count:
# with pathologically long lines the block-doubling below would otherwise
# grow until it holds `lines` newlines, which can mean the whole (multi-GB)
# file. Stop here and return the last ~this-many bytes' worth of lines.
_LOG_TAIL_MAX_BYTES = 32 * 1024 * 1024
_LOG_MAX_LINES = 5000
_LOG_DEFAULT_LINES = 200


def lane_log_path(lane_id):
    """Absolute path of the lane's log file, or None if the lane declares none.

    SCRIPT_LANES holds the lane dicts after config.load(), which already
    expanded `~` and resolved repo-relative paths, so the value here is
    absolute whenever a `log` key is present. Lanes without one have no
    reliable log location (the convention is
    $HOME/.hermes/logs/<something>.log, but nothing maps lane -> filename),
    so we return None rather than guess.
    """
    lane = SCRIPT_LANES.get(lane_id)
    if not lane:
        return None
    log = lane.get("log")
    if not log:
        return None
    return os.path.expanduser(log)


def _clamp_lines(lines):
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        return _LOG_DEFAULT_LINES
    return max(1, min(_LOG_MAX_LINES, lines))


def read_log_tail(lane_id, lines=_LOG_DEFAULT_LINES):
    """Return (path, exists, tail) for a lane's log.

    `path` is the absolute log path or None when the lane declares none.
    `exists` is whether that path is a readable file; `tail` is its last
    `lines` lines (clamped to 1..5000) or "" when unreadable. `lines` is
    accepted loosely (a query string, None, garbage) and coerced, so the
    HTTP layer can pass it straight through.
    """
    path = lane_log_path(lane_id)
    if not path:
        return None, False, ""
    lines = _clamp_lines(lines)
    if not os.path.isfile(path):
        return path, False, ""
    try:
        with open(path, "rb") as f:
            try:
                size = os.fstat(f.fileno()).st_size
            except OSError:
                size = None
            if size is not None and size > _LOG_TAIL_READ_THRESHOLD:
                # Back off from the end by more than lines * 1024 bytes to
                # tolerate very long lines, re-reading from further back
                # until the block holds enough lines (or the whole file).
                # data is kept disjoint (each pass replaces it with the
                # superset it read), so bytes are never counted twice.
                block = lines * 1024 + 1024
                data = b""
                while True:
                    start = max(0, size - block)
                    f.seek(start)
                    data = f.read(size - start)
                    chunks = data.split(b"\n")
                    if len(chunks) > lines + 1 or start == 0:
                        break
                    # Cap the read so a log of very long lines can't make us
                    # grow the block to the whole (multi-GB) file.
                    if block >= _LOG_TAIL_MAX_BYTES:
                        break
                    block *= 2
                # split gives one more element than line breaks; the first
                # is empty when the block starts at a line boundary.
                out = b"\n".join(chunks[-(lines + 1):])
            else:
                out = f.read()
        text = out.decode("utf-8", "replace")
        body = text.split("\n")
        if body and body[-1] == "":
            body.pop()  # trailing newline is not a line
        return path, True, "\n".join(body[-lines:])
    except OSError:
        return path, False, ""


def api_config():
    """Everything the frontends need to stop hardcoding ports.

    { api_base, uzu_port, panel_port, lanes:[{id,label,port,kind,approx_mb}] }
    """
    return {
        "api_base": f"http://{HOST}:{PORT}",
        "uzu_port": uzu_proxy_port(),
        "panel_port": PORT,
        "lanes": [{
            "id": l["id"],
            "label": l.get("label", l["id"]),
            "port": l.get("port"),
            "kind": l.get("kind", "script"),
            "approx_mb": l.get("approx_mb"),
            "model": l.get("model"),  # optional per-lane model id for the chat
        } for l in SCRIPT_LANES.values()],
    }


# --- Graceful drain ---------------------------------------------------------
#
# Stopping a lane used to be a hard `pkill`, which severs an active chat
# stream mid-token. Before the script's stop runs, toggle() now waits
# (bounded) for in-flight proxied requests to finish: the SSE proxy in
# thrash_server counts each streaming request against its lane for the
# duration of the stream, and drain() polls that counter to zero.
#
# DEFAULT ON and harmless when nothing is proxied: an idle lane's counter is
# absent, so drain() returns immediately and the stop path is unchanged.

ACTIVE_REQUESTS = {}  # lane_id -> count of in-flight proxied requests

# How long toggle(off) waits for in-flight requests before stopping anyway.
DRAIN_TIMEOUT_S = 15


def begin_request(lane_id):
    """Count one in-flight proxied request against lane_id."""
    if lane_id is None:
        return
    ACTIVE_REQUESTS[lane_id] = ACTIVE_REQUESTS.get(lane_id, 0) + 1


def end_request(lane_id):
    """Release one in-flight request; the count floors at zero."""
    if lane_id is None:
        return
    remaining = ACTIVE_REQUESTS.get(lane_id, 0) - 1
    if remaining > 0:
        ACTIVE_REQUESTS[lane_id] = remaining
    else:
        ACTIVE_REQUESTS.pop(lane_id, None)


def drain(lane_id, timeout_s=DRAIN_TIMEOUT_S):
    """Wait up to timeout_s for lane_id's in-flight requests to reach zero.

    Returns True when the lane is quiet within the budget, False when the
    timeout expires with requests still active. Polls every 0.25s and
    returns immediately when nothing is in flight, so an idle lane costs
    no wait at all.
    """
    deadline = time.time() + timeout_s
    while ACTIVE_REQUESTS.get(lane_id, 0) > 0:
        if time.time() >= deadline:
            return False
        time.sleep(0.25)
    return True


def toggle(lane_id, turn_on):
    if lane_id in SCRIPT_LANES:
        lane = SCRIPT_LANES[lane_id]
        script = lane["script"]
        if turn_on:
            # RAM-projection guard runs BEFORE spawning: a refusal must never
            # leave a stray process behind. `projected` doubles as the record
            # of what was projected, so messages quote the projected headroom
            # without a second get_status() round trip. It is not None here
            # only when would_thrash is True (project_start returns None with
            # would_thrash False).
            projected, would_thrash, _ = project_start(lane)
            if would_thrash and ram_guard_mode() == "block":
                cost = lane.get("approx_mb") or 0
                free = (projected or 0) + cost  # projected == free - cost
                return False, (f"refused: starting {lane_id} needs ~{cost}MB "
                               f"but only {free}MB free "
                               f"(would leave ~{projected}MB)")
            warn_note = (
                f"WARNING: only ~{projected}MB would remain — " if would_thrash
                else ""
            )
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
            return True, f"{warn_note}{lane_id} starting (loads in background)"
        # Graceful drain: let in-flight proxied requests finish before the
        # script hard-stops the process, so stopping a lane mid-chat does
        # not sever an active stream. Bounded — a wedged request can never
        # hang the panel, we just report what was still in flight. The
        # timeout is read at call time (not bound as a default) so a test
        # or config can shorten the wait.
        drained = drain(lane_id, timeout_s=DRAIN_TIMEOUT_S)
        active = ACTIVE_REQUESTS.get(lane_id, 0)
        rc, out, err = run([script, "stop"], timeout=20)
        msg = out.strip() or err.strip() or f"stop exited {rc}"
        if drained:
            msg = f"{msg} (drained)"
        elif active:
            msg = (f"{msg} (stopped with {active} request(s) still active "
                   f"after {DRAIN_TIMEOUT_S}s drain)")
        return rc == 0, msg
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


def chat_lane_port(lane_id=None):
    """Resolve the port the chat proxy should target.

    With no ``lane_id`` (or None) this returns the configured uzu_proxy_lane's
    port (backward compatible). With a ``lane_id`` it returns that script
    lane's port, or None if the id is unknown — so the caller can 400 on a
    bad lane vs 502 on a missing default. Only script lanes are valid targets
    (ramtoggle lanes aren't OpenAI-servable by us).
    """
    if not lane_id:
        return uzu_proxy_port()
    lane = SCRIPT_LANES.get(lane_id)
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


# --- Idle auto-unload (TTL reaper) -----------------------------------------
#
# Activity is tracked in-process: the Thrash server records a timestamp for a
# lane whenever it mediates a request against it (a toggle-on or an uzu-chat
# proxied message). The reaper thread (server/reaper.py) periodically asks
# reaper_candidates() which enabled, currently-on script lanes have been idle
# longer than the configured TTL, and stops them.
#
# DEFAULT OFF: with idle_unload.enabled = false (the config default) nothing
# here changes behaviour — reaper_candidates() returns [] and the reaper
# thread is never started. When a lane has never recorded activity,
# last_active() is None and it counts as idle since the epoch, so enabling
# TTL promptly unloads a running lane that has had no Thrash-mediated
# activity since the server started (documented behaviour).

ACTIVITY = {}  # lane_id -> epoch seconds of last Thrash-mediated activity


def touch_activity(lane_id):
    """Record `now` as the last activity time for lane_id."""
    if lane_id is not None:
        ACTIVITY[lane_id] = time.time()


def last_active(lane_id):
    """Last recorded activity time for lane_id, or None if never seen."""
    return ACTIVITY.get(lane_id)


def idle_unload_cfg():
    """The idle_unload config block (defaulted by config.load())."""
    return CFG.get("idle_unload") or {
        "enabled": False, "ttl_minutes": 30, "lanes": ["*"]}


def reaper_candidates(now=None):
    """Ids of script lanes that should be stopped for idleness.

    A lane is a candidate when idle-unload is enabled, the lane is in scope
    (lanes == ["*"] or listed), the lane script reports UP, and the time
    since its last recorded activity exceeds ttl_minutes. A lane with no
    recorded activity is treated as idle since the epoch.
    """
    if now is None:
        now = time.time()
    cfg = idle_unload_cfg()
    if not cfg.get("enabled"):
        return []
    lanes = cfg.get("lanes") or ["*"]
    ttl_s = cfg.get("ttl_minutes", 30) * 60
    candidates = []
    for lane_id, lane in SCRIPT_LANES.items():
        if "*" not in lanes and lane_id not in lanes:
            continue
        if not script_lane_on(lane):
            continue
        last = last_active(lane_id) or 0
        if (now - last) > ttl_s:
            candidates.append(lane_id)
    return candidates


def stop_idle_lanes(now=None):
    """Stop every reaper_candidates() lane; clear its activity record.

    Returns [(lane_id, ok, msg), ...] — same shape as toggle()'s result —
    using the identical stop path (`<script> stop`). No threads live here;
    the decision logic is plain functions so it is testable directly.
    """
    results = []
    for lane_id in reaper_candidates(now=now):
        lane = SCRIPT_LANES[lane_id]
        rc, out, err = run([lane["script"], "stop"], timeout=20)
        ok = rc == 0
        msg = out.strip() or err.strip() or f"stop exited {rc}"
        ACTIVITY.pop(lane_id, None)
        results.append((lane_id, ok, msg))
    return results
