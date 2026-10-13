#!/usr/bin/env python3
"""
reaper.py — idle auto-unload for Thrash.

A daemon thread that periodically stops script lanes which have seen no
Thrash-mediated activity for longer than the configured TTL, so ad-hoc use
of the panel cannot leak RAM overnight.

All the decision logic lives in core (reaper_candidates / stop_idle_lanes)
so it is testable without threads; this module is only the timing loop:

    while running:
        sleep(interval_s)
        core.stop_idle_lanes()

DEFAULT OFF: the caller only starts a Reaper when
core.CFG["idle_unload"]["enabled"] is true, so with the stock config
(default disabled) no thread exists and behaviour is unchanged.

Stdlib only.
"""
import threading
import time

import core


class Reaper:
    """Daemon thread calling core.stop_idle_lanes() every interval_s.

    start()  — spawn the daemon thread (idempotent; a no-op if running)
    stop()   — ask the loop to exit after its current sleep and join it
    """

    def __init__(self, interval_s=60):
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Spawn the reaper thread unless it is already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="thrash-reaper", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            # Sleep first: a lane just started should not be reaped before it
            # has had a chance to register activity. Event.wait() returns
            # early (and truthy) when stop() sets it, so shutdown is prompt.
            if self._stop.wait(self.interval_s):
                break
            core.stop_idle_lanes()

    def stop(self):
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 5)
            self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


if __name__ == "__main__":
    # Manual run: reaps every minute against whatever config is present.
    # Ctrl-C exits (daemon thread dies with the process).
    cfg = core.idle_unload_cfg()
    print(f"reaper: enabled={cfg.get('enabled')} "
          f"ttl={cfg.get('ttl_minutes')}min lanes={cfg.get('lanes')}",
          flush=True)
    r = Reaper()
    r.start()
    try:
        while r.running:
            time.sleep(1)
    except KeyboardInterrupt:
        r.stop()
