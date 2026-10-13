"""Tests for server/notify.py — best-effort macOS notifications.

No real osascript is ever invoked: notify_send's subprocess.run is
monkeypatched (per test, restored in tearDown) to capture the argv it
builds. Core's config block, script_lane_on and get_status are stubbed the
same way, so the watchers' logic is exercised without spawning lane
processes or reading real memory.

Threads ARE used here (the load-watcher and low-memory watcher run real
daemon threads), but with injectable tiny interval/timeout so every test
finishes in milliseconds. The import test asserts the server spawns none.
"""
import subprocess
import tempfile
import threading
import unittest

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

# Import against a throwaway config so core's module-level CFG never reads
# the machine's real config.local.json.
_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import core      # noqa: E402
import notify    # noqa: E402

helpers.restore_config()


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


class NotifyTestCase(unittest.TestCase):
    """Patches notify.notify_send to capture calls instead of firing
    osascript, and restores everything in tearDown."""

    def setUp(self):
        self.sent = []
        self._orig_send = notify.notify_send
        self._orig_cfg_notif = core.CFG.get("notifications")
        notify.notify_send = lambda title, message: (
            self.sent.append((title, message)) or True)

    def tearDown(self):
        notify.notify_send = self._orig_send
        if self._orig_cfg_notif is not None:
            core.CFG["notifications"] = self._orig_cfg_notif
        else:
            core.CFG.pop("notifications", None)

    def set_notif_cfg(self, on_load=True, on_low_memory=True, low_memory_gb=6):
        core.CFG["notifications"] = {
            "on_load": on_load, "on_low_memory": on_low_memory,
            "low_memory_gb": low_memory_gb}


class NotifySendTest(unittest.TestCase):
    """(a) argv construction + escaping; (b) subprocess failure -> False."""

    def setUp(self):
        self.calls = []
        self._orig_run = subprocess.run
        # notify.py imported subprocess itself; patch there, not on subprocess.
        self._orig_notify_run = notify.subprocess.run

        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")
        notify.subprocess.run = fake_run
        # Isolate the platform check so these tests are deterministic on any OS.
        self._orig_is_macos = notify.IS_MACOS
        notify.IS_MACOS = True

    def tearDown(self):
        notify.subprocess.run = self._orig_notify_run
        notify.IS_MACOS = self._orig_is_macos

    def test_builds_correct_osascript_argv(self):
        ok = notify.notify_send("Thrash", "lane1 is up")
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        argv, kwargs = self.calls[0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[1], "-e")
        self.assertEqual(argv[2],
                         'display notification "lane1 is up" '
                         'with title "Thrash"')
        # No shell=True anywhere — must be a list-args call.
        self.assertNotIn("shell", kwargs or {})

    def test_escapes_quotes_and_backslashes(self):
        notify.notify_send('He said "hi"', 'a\\b"c')
        argv = self.calls[0][0]
        self.assertEqual(
            argv[2],
            'display notification "a\\\\b\\"c" with title "He said \\"hi\\""')

    def test_returns_false_when_subprocess_raises(self):
        def boom(argv, **kwargs):
            raise FileNotFoundError("osascript missing (simulated)")
        notify.subprocess.run = boom
        # Must return False, never raise (simulates non-macOS / no osascript).
        self.assertFalse(notify.notify_send("Thrash", "x"))

    def test_returns_false_when_nonzero_exit(self):
        notify.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "err")
        self.assertFalse(notify.notify_send("Thrash", "x"))

    def test_non_macos_returns_false_without_calling_osascript(self):
        notify.IS_MACOS = False
        self.assertFalse(notify.notify_send("Thrash", "x"))
        self.assertEqual(self.calls, [])


class LaneLoadedTest(NotifyTestCase):
    """(c) watch_lane_load notifies once the lane becomes up."""

    def setUp(self):
        super().setUp()
        self._orig_lane_on = core.script_lane_on
        self._orig_lanes = core.SCRIPT_LANES

    def tearDown(self):
        core.script_lane_on = self._orig_lane_on
        core.SCRIPT_LANES = self._orig_lanes
        super().tearDown()

    def test_notifies_when_lane_becomes_up(self):
        lane = {"id": "lane1", "script": "/bin/true"}
        core.SCRIPT_LANES = {"lane1": lane}
        polls = {"n": 0}

        def fake_on(l):
            polls["n"] += 1
            return polls["n"] >= 3  # up on the 3rd poll
        core.script_lane_on = fake_on

        t = notify.watch_lane_load("lane1", interval=0.01, timeout=5)
        self.assertIsNotNone(t)
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.sent, [("Thrash", "lane1 is up")])

    def test_accepts_lane_dict_directly(self):
        core.SCRIPT_LANES = {"lane2": {"id": "lane2", "script": "/bin/true"}}
        core.script_lane_on = lambda l: True
        t = notify.watch_lane_load({"id": "lane2", "script": "/bin/true"},
                                   interval=0.01, timeout=5)
        self.assertIsNotNone(t)
        t.join(timeout=5)
        self.assertEqual(self.sent, [("Thrash", "lane2 is up")])

    def test_timeout_without_up_sends_nothing(self):
        core.script_lane_on = lambda l: False
        t = notify.watch_lane_load("lane1", {"id": "lane1"},
                                   interval=0.01, timeout=0.05)
        t.join(timeout=5)
        self.assertEqual(self.sent, [])

    def test_disabled_on_load_never_notifies(self):
        self.set_notif_cfg(on_load=False)
        core.SCRIPT_LANES = {"lane1": {"id": "lane1"}}
        core.script_lane_on = lambda l: True
        t = notify.watch_lane_load("lane1", interval=0.01, timeout=1)
        self.assertIsNone(t)
        self.assertEqual(self.sent, [])

    def test_unknown_lane_returns_none(self):
        core.SCRIPT_LANES = {}
        self.assertIsNone(notify.watch_lane_load("ghost", interval=0.01))


class LowMemoryWatchTest(NotifyTestCase):
    """(d) fires once on the not-low -> low edge, not on every sweep."""

    def setUp(self):
        super().setUp()
        self._orig_get_status = core.get_status
        self.watcher = None

    def tearDown(self):
        core.get_status = self._orig_get_status
        if self.watcher is not None:
            self.watcher.stop()
        super().tearDown()

    @staticmethod
    def _status(free_mb):
        return {"memory": {"free_mb": free_mb}, "lanes": []}

    def test_fires_once_on_edge_not_repeatedly(self):
        # below 6GB*1024 after the first two sweeps, then stays low.
        readings = [self._status(20000), self._status(4000),
                    self._status(3000), self._status(3500)]
        core.get_status = lambda: readings.pop(0) if readings \
            else self._status(3000)
        watcher = notify.LowMemoryWatcher(interval=0.01)
        self.watcher = watcher
        watcher._low = False
        # Drive the sweep function directly (the thread's loop is one wait +
        # one check) — deterministic, no timing dependence.
        watcher.check()  # not low -> nothing
        watcher.check()  # edge -> notify
        watcher.check()  # still low -> silent
        watcher.check()  # still low -> silent
        self.assertEqual(len(self.sent), 1)
        title, msg = self.sent[0]
        self.assertEqual(title, "Thrash")
        self.assertIn("Low memory", msg)
        self.assertIn("3.9", msg)  # 4000MB -> 3.9GB

    def test_re_notifies_after_recovery_then_drop(self):
        core.get_status = lambda: self._status(1000)
        watcher = notify.LowMemoryWatcher(interval=0.01)
        self.watcher = watcher
        watcher.check()
        self.assertEqual(len(self.sent), 1)
        core.get_status = lambda: self._status(20000)
        watcher.check()  # recovered -> latch resets
        core.get_status = lambda: self._status(1000)
        watcher.check()  # new crossing -> notify again
        self.assertEqual(len(self.sent), 2)

    def test_disabled_on_low_memory_never_notifies(self):
        self.set_notif_cfg(on_low_memory=False)
        core.get_status = lambda: self._status(1000)
        watcher = notify.LowMemoryWatcher(interval=0.01)
        self.watcher = watcher
        watcher.check()
        self.assertEqual(self.sent, [])

    def test_missing_free_mb_is_noop(self):
        core.get_status = lambda: {"memory": {}, "lanes": []}
        watcher = notify.LowMemoryWatcher(interval=0.01)
        self.watcher = watcher
        self.assertFalse(watcher.check())
        self.assertEqual(self.sent, [])

    def test_watcher_thread_lifecycle(self):
        core.get_status = lambda: self._status(20000)
        watcher = notify.LowMemoryWatcher(interval=0.01)
        watcher.start()
        self.assertTrue(watcher.running)
        watcher.stop()
        self.assertFalse(watcher.running)

    def test_start_low_memory_watch_reuses_running_watcher(self):
        core.get_status = lambda: self._status(20000)
        w1 = notify.start_low_memory_watch(interval=0.01)
        try:
            w2 = notify.start_low_memory_watch(interval=0.01)
            self.assertIs(w1, w2)
        finally:
            w1.stop()
        notify._LOW_MEM_WATCHER = None


class ImportNoThreadsTest(unittest.TestCase):
    """(e) importing thrash_server must spawn no notify threads."""

    def test_import_spawns_no_notify_threads(self):
        before = {t.name for t in threading.enumerate()}
        _tmp = tempfile.TemporaryDirectory()
        helpers.write_config(_tmp.name, [], ramtoggle=None)
        import thrash_server  # noqa: F401
        helpers.restore_config()
        try:
            after = {t.name for t in threading.enumerate()}
            new = after - before
            self.assertNotIn("thrash-notify-lowmem", new)
            self.assertFalse(any(n.startswith("thrash-notify-")
                                 for n in new))
            self.assertNotIn("thrash-reaper", new)
        finally:
            _tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
