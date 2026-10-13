"""Tests for core's graceful drain (in-flight request tracking + drain()).

When a script lane is toggled off, core.drain() waits (bounded) for in-flight
proxied requests to finish before the lane script stops the process, so an
active chat stream is not severed mid-token. The SSE proxy in
thrash_server counts each streaming request via begin_request/end_request.

These tests drive core directly (the drain logic lives there, not in the HTTP
surface). Per-test, core.ACTIVE_REQUESTS / core.SCRIPT_LANES are swapped and
restored, and any spawned stub process is stopped via addCleanup.
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

helpers.write_config(tempfile.mkdtemp(), [], ramtoggle=None)
import core  # noqa: E402

helpers.restore_config()


class DrainStateTestCase(unittest.TestCase):
    """Base: save/restore the core globals the drain tests mutate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_active = core.ACTIVE_REQUESTS
        self._orig_script_lanes = core.SCRIPT_LANES
        self._orig_ramtoggle = core.RAMTOGGLE
        core.ACTIVE_REQUESTS = {}

    def tearDown(self):
        core.ACTIVE_REQUESTS = self._orig_active
        core.SCRIPT_LANES = self._orig_script_lanes
        core.RAMTOGGLE = self._orig_ramtoggle
        self._tmp.cleanup()

    def stub_lane(self, name="lane1", port=8811):
        """Install a stub script lane in core.SCRIPT_LANES; returns the lane."""
        script = helpers.make_lane_script(self.tmpdir, port, name + ".sh")
        lane = {"id": name, "label": name, "kind": "script",
                "port": port, "approx_mb": 1000, "impact": "", "script": script}
        core.SCRIPT_LANES = {name: lane}
        core.RAMTOGGLE = None
        # Never let a stub start process outlive the test.
        self.addCleanup(core.run, [script, "stop"])
        return lane


class BeginEndRequestTest(DrainStateTestCase):
    def test_begin_end_round_trip_to_zero(self):
        core.begin_request("a")
        core.begin_request("a")
        self.assertEqual(core.ACTIVE_REQUESTS["a"], 2)
        core.end_request("a")
        self.assertEqual(core.ACTIVE_REQUESTS["a"], 1)
        core.end_request("a")
        # Count is removed entirely once it hits zero (no stale 0 entries).
        self.assertNotIn("a", core.ACTIVE_REQUESTS)

    def test_end_floors_at_zero(self):
        """Unbalanced end_request must not drive the count negative."""
        core.end_request("never-started")
        self.assertNotIn("never-started", core.ACTIVE_REQUESTS)

    def test_begin_none_is_ignored(self):
        """A missing lane_id (no uzu_proxy_lane configured) never counts."""
        core.begin_request(None)
        self.assertEqual(core.ACTIVE_REQUESTS, {})

    def test_lanes_counted_independently(self):
        core.begin_request("a")
        core.begin_request("b")
        core.begin_request("b")
        self.assertEqual(core.ACTIVE_REQUESTS["a"], 1)
        self.assertEqual(core.ACTIVE_REQUESTS["b"], 2)


class DrainTest(DrainStateTestCase):
    def test_drain_idle_lane_returns_true_immediately(self):
        """An idle lane (no counter) drains without waiting."""
        start = time.time()
        self.assertTrue(core.drain("idle", timeout_s=5))
        self.assertLess(time.time() - start, 0.2)

    def test_drain_after_end_request_returns_true(self):
        """begin -> end before drain: drain returns True without waiting."""
        core.begin_request("a")
        core.end_request("a")
        start = time.time()
        self.assertTrue(core.drain("a", timeout_s=5))
        self.assertLess(time.time() - start, 0.2)

    def test_drain_times_out_when_request_never_ends(self):
        """A wedged request: drain returns False after ~the timeout."""
        core.begin_request("stuck")
        timeout_s = 0.5
        start = time.time()
        self.assertFalse(core.drain("stuck", timeout_s=timeout_s))
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, timeout_s * 0.9)
        self.assertLess(elapsed, timeout_s * 0.9 + 2.0)

    def test_drain_waits_for_request_that_ends_soon(self):
        """A request ending mid-drain unblocks it with True."""
        core.begin_request("slow")

        def finish():
            time.sleep(0.6)
            core.end_request("slow")

        threading.Thread(target=finish, daemon=True).start()
        start = time.time()
        self.assertTrue(core.drain("slow", timeout_s=5))
        self.assertGreaterEqual(time.time() - start, 0.4)


class ToggleDrainIntegrationTest(DrainStateTestCase):
    def test_toggle_off_with_active_request_that_drains(self):
        """toggle(off) waits for the in-flight request, then stops cleanly."""
        lane = self.stub_lane()
        core.ACTIVE_REQUESTS[lane["id"]] = 1

        def finish():
            time.sleep(0.6)
            core.end_request(lane["id"])

        threading.Thread(target=finish, daemon=True).start()
        ok, msg = core.toggle(lane["id"], False)
        self.assertTrue(ok)
        self.assertIn("stopped", msg)
        self.assertIn("drained", msg)

    def test_toggle_off_with_request_that_never_ends_still_stops(self):
        """A wedged request cannot hang the stop: it proceeds after the
        timeout and reports the still-active count."""
        lane = self.stub_lane()
        core.ACTIVE_REQUESTS[lane["id"]] = 2
        # Shorten the wait so the test does not block for the default 15s.
        self.addCleanup(setattr, core, "DRAIN_TIMEOUT_S", core.DRAIN_TIMEOUT_S)
        core.DRAIN_TIMEOUT_S = 0.5
        budget = core.DRAIN_TIMEOUT_S
        start = time.time()
        ok, msg = core.toggle(lane["id"], False)
        elapsed = time.time() - start
        self.assertTrue(ok)
        self.assertIn("stopped", msg)
        self.assertNotIn("drained", msg)
        self.assertIn("2 request(s) still active", msg)
        # The stop actually happened despite the live request.
        self.assertFalse(core.script_lane_on(lane))
        # It waited roughly the drain budget, not the full default.
        self.assertLess(elapsed, budget + 5)

    def test_toggle_off_idle_lane_unchanged_shape(self):
        """No in-flight traffic: the (out/err) message logic is preserved
        and the drain note is appended."""
        lane = self.stub_lane()
        ok, msg = core.toggle(lane["id"], False)
        self.assertTrue(ok)
        self.assertIn("stopped", msg)
        self.assertIn("drained", msg)

    def test_stop_path_not_run_for_ramtoggle_lanes(self):
        """Drain/stop is a script-lane concern only; the ramtoggle path is
        untouched (no counter, no drain note)."""
        stub = helpers.make_ramtoggle_stub(self.tmpdir)
        core.SCRIPT_LANES = {}
        core.RAMTOGGLE = stub
        ok, msg = core.toggle("stub-lane", False)
        self.assertTrue(ok)
        self.assertIn("toggled stub-lane off", msg)
        self.assertNotIn("drained", msg)


if __name__ == "__main__":
    unittest.main()
