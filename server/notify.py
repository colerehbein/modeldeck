#!/usr/bin/env python3
"""
notify.py — best-effort macOS notifications for Thrash.

Two kinds of notification:

  1. lane finished loading  — watch_lane_load() spawns a daemon thread that
     polls core.script_lane_on() until the lane reports UP (or the timeout
     expires), then fires a "Thrash: <lane> is up" notification.
  2. memory is low          — a daemon thread (LowMemoryWatcher) that
     periodically reads core.get_status()["memory"]["free_mb"] and notifies
     ONCE per not-low -> low crossing, so a sustained squeeze does not spam.

Both are gated by the notifications config block (config.py defaults):
    CFG["notifications"] = {"on_load": bool, "on_low_memory": bool,
                            "low_memory_gb": number}

Everything here is best-effort: notify_send() wraps the osascript call in
try/except and returns False on any failure (non-macOS, osascript missing,
permission denied). No caller may ever crash because a notification failed.

NOTHING RUNS ON IMPORT — importing this module spawns no threads. Threads
start only when the caller explicitly invokes watch_lane_load() or
start_low_memory_watch(), which server/thrash_server.py does under __main__
(and in the toggle handler for an individual lane).

Stdlib only.
"""
import subprocess
import sys
import threading
import time

import core

# macOS only; silently no-op everywhere else (osascript exists nowhere else).
IS_MACOS = sys.platform == "darwin"

# Lane-load poll defaults: a cold model load can take a couple of minutes.
LANE_LOAD_INTERVAL_S = 2.0
LANE_LOAD_TIMEOUT_S = 120.0
# Low-memory sweep period.
LOW_MEMORY_INTERVAL_S = 60.0


def _notif_cfg():
    """The notifications config block (read live so tests can patch CFG)."""
    return core.CFG.get("notifications") or {}


def _escape_applescript(s):
    """Escape a Python string for use inside an AppleScript "..." literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def notify_send(title, message):
    """Fire a macOS notification via osascript. Never raises.

    Returns True when osascript accepted the notification, False on any
    failure (non-macOS, missing osascript, non-zero exit, exception).
    """
    if not IS_MACOS:
        return False
    try:
        script = (
            'display notification "{}" with title "{}"'
            .format(_escape_applescript(message), _escape_applescript(title))
        )
        p = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=10)
        return p.returncode == 0
    except Exception:  # noqa: BLE001 — best-effort by contract
        return False


def notify_lane_loaded(lane_id):
    """Announce a finished lane load. No-op when on_load is disabled."""
    if not _notif_cfg().get("on_load", True):
        return False
    return notify_send("Thrash", f"{lane_id} is up")


def watch_lane_load(lane, lane_dict=None, interval=LANE_LOAD_INTERVAL_S,
                    timeout=LANE_LOAD_TIMEOUT_S):
    """Spawn a daemon thread that notifies when `lane` finishes loading.

    `lane` may be the lane id (with `lane_dict` the lane dict from
    core.SCRIPT_LANES) or the lane dict itself. Polls core.script_lane_on()
    every `interval` seconds until it reports True or `timeout` seconds
    elapse. interval/timeout are injectable for tests. Returns the Thread
    (already started; join it in tests); returns None when the load
    notification is disabled or the lane is unknown.

    Never raises — a broken watcher must not break a toggle request.
    """
    if isinstance(lane, dict):
        lane_dict, lane = lane, lane.get("id")
    if lane_dict is None:
        lane_dict = core.SCRIPT_LANES.get(lane)
    if not lane_dict:
        return None
    if not _notif_cfg().get("on_load", True):
        return None
    t = threading.Thread(
        target=_watch_lane_load_loop,
        args=(lane, lane_dict, interval, timeout),
        name=f"thrash-notify-{lane}", daemon=True)
    t.start()
    return t


def _watch_lane_load_loop(lane_id, lane_dict, interval, timeout):
    """Thread body for watch_lane_load: poll until UP or timeout, then tell."""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if core.script_lane_on(lane_dict):
                notify_lane_loaded(lane_id)
                return
            time.sleep(interval)
    except Exception:  # noqa: BLE001 — background thread, never propagate
        pass


class LowMemoryWatcher:
    """Daemon thread that notifies once per not-low -> low memory crossing.

    The edge latch means a sustained low-memory condition notifies a single
    time; the flag resets only when free memory climbs back above the
    threshold, so each new squeeze notifies again. Config is read per-tick
    (on_low_memory / low_memory_gb), so disabling it at runtime stops the
    notifications without restarting the thread.

    start()/stop() mirror server/reaper.py's Reaper.
    """

    def __init__(self, interval=LOW_MEMORY_INTERVAL_S):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._low = False  # rising-edge latch

    def start(self):
        """Spawn the watcher thread unless it is already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="thrash-notify-lowmem", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            # Sleep first so a freshly-started server does not immediately
            # evaluate a possibly-transient reading. Event.wait() returns
            # early (truthy) on stop(), keeping shutdown prompt.
            if self._stop.wait(self.interval):
                break
            self.check()

    def check(self):
        """One sweep: read free memory, notify on a not-low -> low edge.

        Returns True when a notification was sent this sweep. Never raises.
        """
        try:
            if not _notif_cfg().get("on_low_memory", True):
                return False
            threshold_mb = _notif_cfg().get("low_memory_gb", 6) * 1024
            memory = core.get_status().get("memory") or {}
            free_mb = memory.get("free_mb")
            if free_mb is None:
                return False
            low = free_mb < threshold_mb
            if low and not self._low:
                self._low = True
                notify_send("Thrash",
                            f"Low memory: {free_mb / 1024.0:.1f}GB free")
                return True
            if not low:
                self._low = False
            return False
        except Exception:  # noqa: BLE001 — best-effort by contract
            return False

    def stop(self):
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


# Module-global watcher so start_low_memory_watch() is idempotent within a
# process: repeated calls return the already-running watcher.
_LOW_MEM_WATCHER = None


def start_low_memory_watch(interval=LOW_MEMORY_INTERVAL_S):
    """Start (or reuse) the low-memory watcher daemon thread.

    Returns the LowMemoryWatcher; call .stop() on shutdown.
    """
    global _LOW_MEM_WATCHER
    if _LOW_MEM_WATCHER is not None and _LOW_MEM_WATCHER.running:
        return _LOW_MEM_WATCHER
    watcher = LowMemoryWatcher(interval)
    _LOW_MEM_WATCHER = watcher
    watcher.start()
    return watcher
