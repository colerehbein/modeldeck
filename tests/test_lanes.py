"""Tests for server/thrash_server.py lane-control functions.

thrash_server loads config at import time, so the import happens once at
module level against a throwaway temp config — the machine's real
config.local.json is never read by these tests. Per-test, the module's
SCRIPT_LANES/RAMTOGGLE globals are swapped (and restored in tearDown).
"""
import os
import tempfile
import unittest
from pathlib import Path

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

# Build a throwaway config so the module-level CFG = config.load() in
# thrash_server never touches the live config on this machine.
_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import thrash_server  # noqa: E402

helpers.restore_config()


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


class LaneControlTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_script_lanes = thrash_server.SCRIPT_LANES
        self._orig_ramtoggle = thrash_server.RAMTOGGLE

    def tearDown(self):
        thrash_server.SCRIPT_LANES = self._orig_script_lanes
        thrash_server.RAMTOGGLE = self._orig_ramtoggle
        self._tmp.cleanup()

    def stub_lane(self, name="lane1", port=8801):
        script = helpers.make_lane_script(self.tmpdir, port, name + ".sh")
        lane = {"id": name, "label": name, "kind": "script",
                "port": port, "approx_mb": 1000, "impact": "", "script": script}
        thrash_server.SCRIPT_LANES = {name: lane}
        thrash_server.RAMTOGGLE = None
        return lane


class ScriptLaneOnTest(LaneControlTestCase):
    def test_status_flips_after_start(self):
        """script_lane_on is False before start, True after."""
        lane = self.stub_lane()
        self.assertFalse(thrash_server.script_lane_on(lane))
        rc, _, _ = thrash_server.run([lane["script"], "start"])
        self.assertEqual(rc, 0)
        self.assertTrue(thrash_server.script_lane_on(lane))
        rc, _, _ = thrash_server.run([lane["script"], "stop"])
        self.assertEqual(rc, 0)
        self.assertFalse(thrash_server.script_lane_on(lane))

    def test_missing_script_reports_down_not_crash(self):
        """A lane whose script vanished reads as off, not an exception."""
        lane = {"id": "ghost", "script": os.path.join(self.tmpdir, "gone.sh")}
        self.assertFalse(thrash_server.script_lane_on(lane))


class RunTest(LaneControlTestCase):
    def test_run_echo(self):
        rc, out, err = thrash_server.run(["echo", "hi"])
        self.assertEqual(rc, 0)
        self.assertIn("hi", out)
        self.assertEqual(err, "")

    def test_run_missing_command_returns_error_not_raises(self):
        rc, out, err = thrash_server.run(["definitely-not-a-real-cmd-xyz"])
        self.assertNotEqual(rc, 0)
        self.assertTrue(err)


class ToggleScriptLaneTest(LaneControlTestCase):
    def test_toggle_on_missing_script_fails_gracefully(self):
        """toggle(start) on a nonexistent script returns (False, msg)."""
        thrash_server.SCRIPT_LANES = {
            "ghost": {"id": "ghost", "script": os.path.join(self.tmpdir, "nope.sh")}}
        thrash_server.RAMTOGGLE = None
        ok, msg = thrash_server.toggle("ghost", True)
        self.assertFalse(ok)
        self.assertIn("failed to start", msg)

    def test_toggle_off_running_lane(self):
        """toggle(off) on a started stub lane stops it."""
        lane = self.stub_lane()
        rc, _, _ = thrash_server.run([lane["script"], "start"])
        self.assertEqual(rc, 0)
        self.assertTrue(thrash_server.script_lane_on(lane))
        ok, msg = thrash_server.toggle(lane["id"], False)
        self.assertTrue(ok)
        self.assertIn("stopped", msg)
        self.assertFalse(thrash_server.script_lane_on(lane))

    def test_toggle_lane_from_loaded_config(self):
        """SCRIPT_LANES sourced from config.load() drives toggle end to end."""
        lane_script = helpers.make_lane_script(self.tmpdir, 8805, "cfg-lane.sh")
        helpers.write_config(self.tmpdir, [
            {"id": "cfg-lane", "kind": "script", "port": 8805, "script": lane_script},
        ], ramtoggle=None)
        cfg = helpers.config_mod.load()
        thrash_server.SCRIPT_LANES = {l["id"]: l for l in cfg["lanes"]}
        thrash_server.RAMTOGGLE = cfg.get("ramtoggle") or None
        lane = thrash_server.SCRIPT_LANES["cfg-lane"]

        self.assertFalse(thrash_server.script_lane_on(lane))
        thrash_server.run([lane["script"], "start"])
        self.assertTrue(thrash_server.script_lane_on(lane))
        ok, _ = thrash_server.toggle("cfg-lane", False)
        self.assertTrue(ok)
        self.assertFalse(thrash_server.script_lane_on(lane))


class ToggleUnknownLaneTest(LaneControlTestCase):
    def test_unknown_lane_without_ramtoggle(self):
        thrash_server.SCRIPT_LANES = {}
        thrash_server.RAMTOGGLE = None
        ok, msg = thrash_server.toggle("no-such-lane", True)
        self.assertFalse(ok)
        self.assertIn("unknown lane", msg)

    def test_unknown_lane_not_in_ramtoggle(self):
        stub = helpers.make_ramtoggle_stub(self.tmpdir)
        thrash_server.SCRIPT_LANES = {}
        thrash_server.RAMTOGGLE = stub
        ok, msg = thrash_server.toggle("not-a-ramtoggle-lane", True)
        self.assertFalse(ok)
        self.assertIn("unknown lane", msg)

    def test_ramtoggle_lane_toggles_via_stub(self):
        stub = helpers.make_ramtoggle_stub(self.tmpdir)
        thrash_server.SCRIPT_LANES = {}
        thrash_server.RAMTOGGLE = stub
        ok, msg = thrash_server.toggle("stub-lane", True)
        self.assertTrue(ok)
        self.assertIn("toggled stub-lane on", msg)

        lanes, memory = thrash_server.ramtoggle_status()
        self.assertEqual([l["id"] for l in lanes], ["stub-lane"])
        self.assertEqual(memory["total_mb"], 32768)

    def test_ramtoggle_status_off_when_unset(self):
        thrash_server.RAMTOGGLE = None
        lanes, memory = thrash_server.ramtoggle_status()
        self.assertEqual(lanes, [])
        self.assertEqual(memory, {})


class UzuProxyPortTest(LaneControlTestCase):
    def test_uzu_proxy_port_none_without_lane(self):
        self._orig = thrash_server.UZU_PROXY_LANE
        self.addCleanup(setattr, thrash_server, "UZU_PROXY_LANE", self._orig)
        thrash_server.UZU_PROXY_LANE = None
        self.assertIsNone(thrash_server.uzu_proxy_port())

    def test_uzu_proxy_port_reads_lane_port(self):
        self._orig = thrash_server.UZU_PROXY_LANE
        self.addCleanup(setattr, thrash_server, "UZU_PROXY_LANE", self._orig)
        thrash_server.UZU_PROXY_LANE = "uzu-9b"
        thrash_server.SCRIPT_LANES = {
            "uzu-9b": {"id": "uzu-9b", "port": 8096, "script": "x"}}
        self.assertEqual(thrash_server.uzu_proxy_port(), 8096)


if __name__ == "__main__":
    unittest.main()
