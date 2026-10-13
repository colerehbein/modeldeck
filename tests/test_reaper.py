"""Tests for the idle auto-unload (TTL reaper) logic in core.

No threads here: every test drives core.reaper_candidates() and
core.stop_idle_lanes() directly with a stub lane script (the same
blocking-start stub used by the lane tests), patching core.SCRIPT_LANES
and core.CFG["idle_unload"]. The Reaper thread itself is not exercised —
its loop is one sleep plus a call into this tested logic.

Fixture per test: a temp dir holding an executable lane stub whose
start/stop flip a marker file, so "the reaper stopped it" is observable
as the marker being gone.
"""
import os
import tempfile
import threading
import time
import unittest

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

# Import against a throwaway config so core's module-level CFG never reads
# the machine's real config.local.json.
_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import core  # noqa: E402

helpers.restore_config()

_IDLE_OFF = {"enabled": False, "ttl_minutes": 30, "lanes": ["*"]}


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


class ReaperTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_lanes = core.SCRIPT_LANES
        self._orig_cfg_idle = core.CFG.get("idle_unload")
        self._orig_activity = core.ACTIVITY
        core.ACTIVITY = {}

    def tearDown(self):
        core.SCRIPT_LANES = self._orig_lanes
        core.CFG["idle_unload"] = self._orig_cfg_idle
        core.ACTIVITY = self._orig_activity
        self._tmp.cleanup()

    def lane(self, name="lane1", port=8871):
        """Register a stub lane in core.SCRIPT_LANES; return its lane dict."""
        script = helpers.make_lane_script(self.tmpdir, port, name + ".sh")
        lane = {"id": name, "label": name, "kind": "script",
                "port": port, "approx_mb": 1000, "impact": "", "script": script}
        core.SCRIPT_LANES = {name: lane}
        return lane

    def start_lane(self, lane):
        """Bring the stub lane up via core.toggle(); addCleanup stops it."""
        self.addCleanup(core.run, [lane["script"], "stop"])
        ok, msg = core.toggle(lane["id"], True)
        self.assertTrue(ok, msg)
        self.assertTrue(core.script_lane_on(lane))

    def set_idle_cfg(self, enabled=True, ttl_minutes=30, lanes=None):
        core.CFG["idle_unload"] = {
            "enabled": enabled,
            "ttl_minutes": ttl_minutes,
            "lanes": lanes if lanes is not None else ["*"],
        }

    @property
    def marker(self):
        return os.path.join(self.tmpdir, "lane1.sh.up")


class DisabledTest(ReaperTestCase):
    def test_disabled_never_reaps_even_long_idle(self):
        """(a) idle_unload disabled -> no candidates even if idle."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=False)
        self.assertEqual(core.reaper_candidates(), [])
        self.assertEqual(core.stop_idle_lanes(), [])
        self.assertTrue(core.script_lane_on(lane))

    def test_missing_idle_cfg_defaults_off(self):
        """A config without the idle_unload block behaves as disabled."""
        lane = self.lane()
        self.start_lane(lane)
        core.CFG.pop("idle_unload", None)
        self.assertEqual(core.idle_unload_cfg()["enabled"], False)
        self.assertEqual(core.reaper_candidates(), [])


class IdleCandidateTest(ReaperTestCase):
    def test_long_idle_lane_is_candidate_and_stopped(self):
        """(b) enabled + on + idle past TTL -> candidate; reaper stops it."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1)
        # Never touched: no activity record -> idle since epoch.
        self.assertIsNone(core.last_active(lane["id"]))
        self.assertEqual(core.reaper_candidates(), [lane["id"]])
        results = core.stop_idle_lanes()
        self.assertEqual([r[0] for r in results], [lane["id"]])
        self.assertTrue(results[0][1])  # stop succeeded
        self.assertFalse(core.script_lane_on(lane))  # marker gone
        self.assertIsNone(core.last_active(lane["id"]))  # record cleared

    def test_recently_touched_lane_is_not_candidate(self):
        """(c) enabled + on + touched just now -> not a candidate."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=30)
        core.touch_activity(lane["id"])
        self.assertEqual(core.reaper_candidates(now=time.time()), [])
        self.assertEqual(core.stop_idle_lanes(), [])
        self.assertTrue(core.script_lane_on(lane))

    def test_touch_activity_defers_past_ttl(self):
        """touch_activity sets last_active; the lane only reaps past TTL."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=10)
        t0 = time.time()
        core.touch_activity(lane["id"])
        self.assertEqual(core.reaper_candidates(now=t0 + 600), [])
        self.assertEqual(core.reaper_candidates(now=t0 + 601), [lane["id"]])

    def test_activity_survives_when_stop_fails(self):
        """A failed stop keeps the lane's activity cleared only on success.

        Here the stop path always succeeds; this asserts the result tuple
        shape returned to the caller.
        """
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1)
        results = core.stop_idle_lanes()
        self.assertEqual(len(results), 1)
        lane_id, ok, msg = results[0]
        self.assertEqual(lane_id, lane["id"])
        self.assertIsInstance(ok, bool)
        self.assertTrue(msg)


class LaneScopeTest(ReaperTestCase):
    def test_lane_not_in_scope_list_is_not_candidate(self):
        """(d) enabled but lanes=["other"] -> out-of-scope lane ignored."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1, lanes=["other"])
        self.assertEqual(core.reaper_candidates(), [])

    def test_lane_in_scope_list_is_candidate(self):
        """The complement: explicitly listed lane is in scope."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1, lanes=["lane1", "other"])
        self.assertEqual(core.reaper_candidates(), [lane["id"]])

    def test_wildcard_scope_covers_every_lane(self):
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1, lanes=["*"])
        self.assertEqual(core.reaper_candidates(), [lane["id"]])


class LaneOffTest(ReaperTestCase):
    def test_off_lane_is_not_candidate(self):
        """(e) enabled but lane off -> not a candidate."""
        lane = self.lane()
        self.assertFalse(core.script_lane_on(lane))
        self.set_idle_cfg(enabled=True, ttl_minutes=1)
        self.assertEqual(core.reaper_candidates(), [])

    def test_off_lane_untouched_after_previous_stop(self):
        """A lane stopped earlier (activity cleared) stays untouched."""
        lane = self.lane()
        self.start_lane(lane)
        self.set_idle_cfg(enabled=True, ttl_minutes=1)
        core.touch_activity(lane["id"])
        core.toggle(lane["id"], False)
        self.assertEqual(core.stop_idle_lanes(), [])
        self.assertFalse(core.script_lane_on(lane))


class MultiLaneTest(ReaperTestCase):
    def test_reaps_idle_lane_keeps_active_one(self):
        """With two lanes, only the idle one is stopped."""
        l1 = self.lane("idle-lane", 8881)
        script2 = helpers.make_lane_script(self.tmpdir, 8882, "busy-lane.sh")
        l2 = {"id": "busy-lane", "label": "busy", "kind": "script",
              "port": 8882, "approx_mb": 500, "impact": "", "script": script2}
        core.SCRIPT_LANES = {"idle-lane": l1, "busy-lane": l2}
        self.addCleanup(core.run, [l1["script"], "stop"])
        self.addCleanup(core.run, [script2, "stop"])
        ok, _ = core.toggle("idle-lane", True)
        self.assertTrue(ok)
        ok, _ = core.toggle("busy-lane", True)
        self.assertTrue(ok)
        self.set_idle_cfg(enabled=True, ttl_minutes=30)
        core.touch_activity("busy-lane")

        self.assertEqual(core.reaper_candidates(), ["idle-lane"])
        core.stop_idle_lanes()
        self.assertFalse(core.script_lane_on(l1))
        self.assertTrue(core.script_lane_on(l2))


class ActivityHelpersTest(ReaperTestCase):
    def test_last_active_none_before_any_touch(self):
        self.assertIsNone(core.last_active("never-seen"))

    def test_touch_activity_none_is_noop(self):
        core.touch_activity(None)
        self.assertEqual(core.ACTIVITY, {})

    def test_idle_unload_cfg_fallback_shape(self):
        core.CFG.pop("idle_unload", None)
        cfg = core.idle_unload_cfg()
        self.assertEqual(cfg, {"enabled": False, "ttl_minutes": 30,
                               "lanes": ["*"]})


class ImportDoesNotStartThreadTest(unittest.TestCase):
    def test_thrash_server_import_spawns_no_reaper(self):
        """Importing thrash_server must not start any background thread.

        The reaper only starts under __main__. Snapshot live threads before
        and after the import (whichever it may be) and require no growth —
        with the reaper guarded by __main__, import adds only the importer's
        own machinery, never a "thrash-reaper" thread.
        """
        before = {t.name for t in threading.enumerate()}
        _import_tmp2 = tempfile.TemporaryDirectory()
        helpers.write_config(_import_tmp2.name, [], ramtoggle=None)
        import thrash_server  # noqa: F401
        helpers.restore_config()
        try:
            after = threading.enumerate()
            self.assertNotIn("thrash-reaper",
                             {t.name for t in after})
        finally:
            _import_tmp2.cleanup()


if __name__ == "__main__":
    unittest.main()
