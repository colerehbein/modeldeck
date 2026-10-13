"""Tests for server/thrash_server.py lane-control functions.

thrash_server loads config at import time, so the import happens once at
module level against a throwaway temp config — the machine's real
config.local.json is never read by these tests. Per-test, the module's
SCRIPT_LANES/RAMTOGGLE globals are swapped (and restored in tearDown).
"""
import json
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
        # Start via toggle() (the production entry point), not by calling the
        # script directly: the stub now holds like the real lane scripts, so
        # toggle's poll()-after-0.5s check sees a live process and succeeds.
        self.addCleanup(thrash_server.run, [lane["script"], "stop"])
        ok, _ = thrash_server.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertTrue(thrash_server.script_lane_on(lane))
        ok, _ = thrash_server.toggle(lane["id"], False)
        self.assertTrue(ok)
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
        self.addCleanup(thrash_server.run, [lane["script"], "stop"])
        ok, _ = thrash_server.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertTrue(thrash_server.script_lane_on(lane))
        ok, msg = thrash_server.toggle(lane["id"], False)
        self.assertTrue(ok)
        self.assertIn("stopped", msg)
        self.assertFalse(thrash_server.script_lane_on(lane))

    def test_toggle_on_running_lane_succeeds_and_leaves_marker(self):
        """toggle(start) on a holding stub: success + marker + script_lane_on.

        This is the production start path: the stub holds for STUB_HOLD
        seconds like the real lane scripts, so toggle's 0.5s poll() sees a
        live process. Cleanup stops the lane so no process outlives the test
        (the stub's stop pkills its own start process by script path).
        """
        os.environ["STUB_HOLD"] = "3"
        self.addCleanup(os.environ.pop, "STUB_HOLD", None)
        lane = self.stub_lane()
        self.addCleanup(thrash_server.run, [lane["script"], "stop"])
        marker = os.path.join(self.tmpdir, lane["id"] + ".sh.up")
        self.assertFalse(os.path.exists(marker))
        ok, msg = thrash_server.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertIn("starting", msg)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(thrash_server.script_lane_on(lane))
        # Cleanup already registered; explicitly stop to assert it works.
        ok, msg = thrash_server.toggle(lane["id"], False)
        self.assertTrue(ok)
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
        self.addCleanup(thrash_server.run, [lane["script"], "stop"])

        self.assertFalse(thrash_server.script_lane_on(lane))
        ok, _ = thrash_server.toggle("cfg-lane", True)
        self.assertTrue(ok)
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


class RamtoggleStateRoundTripTest(LaneControlTestCase):
    """The stateful ramtoggle stub: toggle(id, on/off) is observable in --json."""

    def _stub_ramtoggle(self):
        stub = helpers.make_ramtoggle_stub(self.tmpdir)
        thrash_server.SCRIPT_LANES = {}
        thrash_server.RAMTOGGLE = stub
        self.addCleanup(thrash_server.toggle, "stub-lane", False)
        return stub

    def test_toggle_on_then_off_round_trips_through_json(self):
        """toggle(on) flips --json to on:true; toggle(off) back to on:false."""
        self._stub_ramtoggle()
        lanes, _ = thrash_server.ramtoggle_status()
        self.assertFalse(lanes[0]["on"])  # state file absent -> off

        ok, msg = thrash_server.toggle("stub-lane", True)
        self.assertTrue(ok)
        self.assertIn("toggled stub-lane on", msg)
        lanes, _ = thrash_server.ramtoggle_status()
        self.assertTrue(lanes[0]["on"])

        ok, msg = thrash_server.toggle("stub-lane", False)
        self.assertTrue(ok)
        self.assertIn("toggled stub-lane off", msg)
        lanes, _ = thrash_server.ramtoggle_status()
        self.assertFalse(lanes[0]["on"])

    def test_toggle_reflected_in_raw_json_output(self):
        """The on/off flip is visible in the raw --json stdout, not just the
        parsed lanes list."""
        self._stub_ramtoggle()
        before = thrash_server.run([thrash_server.RAMTOGGLE, "--json"])[1]
        self.assertIn('"on":false', before)
        thrash_server.toggle("stub-lane", True)
        after = thrash_server.run([thrash_server.RAMTOGGLE, "--json"])[1]
        self.assertIn('"on":true', after)


class RamtoggleFailureTest(LaneControlTestCase):
    """ramtoggle_status() degrades to ([], {}) on malformed / failed --json."""

    def _ramtoggle_stub_with(self, body):
        stub = helpers.write_exec(
            os.path.join(self.tmpdir, "rt-bad"), "#!/usr/bin/env bash\n" + body)
        thrash_server.SCRIPT_LANES = {}
        thrash_server.RAMTOGGLE = stub
        return stub

    def test_malformed_json_returns_empty_not_raise(self):
        self._ramtoggle_stub_with("echo 'not json at all'\n")
        lanes, memory = thrash_server.ramtoggle_status()
        self.assertEqual(lanes, [])
        self.assertEqual(memory, {})

    def test_empty_output_returns_empty_not_raise(self):
        self._ramtoggle_stub_with("exit 0\n")
        lanes, memory = thrash_server.ramtoggle_status()
        self.assertEqual(lanes, [])
        self.assertEqual(memory, {})

    def test_nonzero_exit_returns_empty_not_raise(self):
        self._ramtoggle_stub_with("echo 'boom' >&2; exit 1\n")
        lanes, memory = thrash_server.ramtoggle_status()
        self.assertEqual(lanes, [])
        self.assertEqual(memory, {})

    def test_toggle_on_known_lane_when_ramtoggle_fails(self):
        """toggle() on a ramtoggle lane whose --json fails -> unknown lane,
        not an exception (the lane list came back empty)."""
        self._ramtoggle_stub_with("exit 1\n")
        ok, msg = thrash_server.toggle("stub-lane", True)
        self.assertFalse(ok)
        self.assertIn("unknown lane", msg)


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
