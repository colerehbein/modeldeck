"""Tests for the RAM-projection guard in core.toggle().

The guard projects memory pressure before a script lane is spawned:
  - ram_guard.mode == "block" + projected thrash -> (False, "refused: ...")
    and the lane is never started (no marker file, no process)
  - ram_guard.mode == "warn"  + projected thrash -> (True, "WARNING: ...")
    and the lane does start
  - ample free memory (either mode)              -> (True, clean message)
  - no free_mb (ramtoggle unavailable)           -> proceeds, never blocks

Driving toggle() requires core.CFG["ram_guard"]["mode"] per test, so the
module-level CFG is patched (and restored) rather than rewriting the config
files on disk — same convention as the other test modules.
"""
import os
import tempfile
import unittest

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

# Import core against a throwaway config so the module-level CFG = config.load()
# never reads the machine's real config.local.json. core inserts server/ on
# sys.path itself; helpers must be imported first so `import config` resolves.
_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import core  # noqa: E402

helpers.restore_config()


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


# Memory states used by the stub-free monkeypatch of core.ramtoggle_status().
# LOW: only 1000MB free, so any lane costing more than that projects below the
# 1500MB threshold. AMPLE: 18000MB of a 32768MB machine — headroom either way.
_LOW = {"free_mb": 1000, "total_mb": 32768}
_AMPLE = {"free_mb": 18000, "total_mb": 32768}
_NO_MEMORY = {}


class RamGuardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_script_lanes = core.SCRIPT_LANES
        self._orig_ramtoggle = core.RAMTOGGLE
        self._orig_cfg = core.CFG
        self._orig_ramtoggle_status = core.ramtoggle_status
        core.RAMTOGGLE = None  # the guard reads memory via ramtoggle_status()

    def tearDown(self):
        core.SCRIPT_LANES = self._orig_script_lanes
        core.RAMTOGGLE = self._orig_ramtoggle
        core.CFG = self._orig_cfg
        core.ramtoggle_status = self._orig_ramtoggle_status
        self._tmp.cleanup()

    def set_memory(self, memory):
        """Monkeypatch core.ramtoggle_status to report a fixed memory dict."""
        core.ramtoggle_status = lambda: ([], memory)

    def set_mode(self, mode):
        """Point core.CFG at a copy whose ram_guard.mode is `mode`."""
        import copy
        cfg = copy.deepcopy(self._orig_cfg)
        cfg.setdefault("ram_guard", {})["mode"] = mode
        core.CFG = cfg

    def stub_lane(self, name="guard-lane", port=8899, approx_mb=20000):
        """A holding script lane stub, registered in core.SCRIPT_LANES."""
        os.environ["STUB_HOLD"] = "3"
        self.addCleanup(os.environ.pop, "STUB_HOLD", None)
        script = helpers.make_lane_script(self.tmpdir, port, name + ".sh")
        lane = {"id": name, "label": name, "kind": "script", "port": port,
                "approx_mb": approx_mb, "impact": "", "script": script}
        core.SCRIPT_LANES = {name: lane}
        # Guard against a lane whose start process outlives the test.
        self.addCleanup(core.run, [script, "stop"])
        return lane, os.path.join(self.tmpdir, name + ".sh.up")


class RamGuardModeTest(RamGuardTestCase):
    def test_default_is_warn(self):
        """A config with no ram_guard block reads as "warn" (old behaviour)."""
        core.CFG = {"server": {}, "lanes": []}
        self.assertEqual(core.ram_guard_mode(), "warn")

    def test_block_and_warn_read_from_cfg(self):
        for mode in ("warn", "block"):
            self.set_mode(mode)
            self.assertEqual(core.ram_guard_mode(), mode)


class ProjectStartTest(RamGuardTestCase):
    def test_none_free_mb_projects_nothing_and_never_thrashes(self):
        self.set_memory(_NO_MEMORY)
        lane = {"approx_mb": 99999}
        self.assertEqual(core.project_start(lane), (None, False, None))

    def test_low_free_projects_thrash(self):
        self.set_memory(_LOW)
        lane = {"approx_mb": 20000}
        projected, would_thrash, total = core.project_start(lane)
        self.assertEqual(projected, -19000)
        self.assertTrue(would_thrash)
        self.assertEqual(total, 32768)

    def test_ample_free_projects_no_thrash(self):
        self.set_memory(_AMPLE)
        lane = {"approx_mb": 2000}
        projected, would_thrash, _ = core.project_start(lane)
        self.assertEqual(projected, 16000)
        self.assertFalse(would_thrash)

    def test_missing_approx_mb_costs_zero(self):
        self.set_memory(_AMPLE)
        projected, would_thrash, _ = core.project_start({"id": "no-cost"})
        self.assertEqual(projected, 18000)
        self.assertFalse(would_thrash)


class RamGuardBlockTest(RamGuardTestCase):
    def test_block_refuses_and_never_starts_lane(self):
        """block + low free + huge lane -> refusal, marker never created."""
        self.set_mode("block")
        self.set_memory(_LOW)
        lane, marker = self.stub_lane()
        self.assertFalse(os.path.exists(marker))

        ok, msg = core.toggle(lane["id"], True)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertIn("guard-lane", msg)
        # The whole point of blocking: nothing was spawned.
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(core.script_lane_on(lane))

    def test_block_allows_ample_free(self):
        self.set_mode("block")
        self.set_memory(_AMPLE)
        lane, marker = self.stub_lane(approx_mb=2000)
        ok, msg = core.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertNotIn("refused", msg)
        self.assertNotIn("WARNING", msg)
        self.assertTrue(os.path.exists(marker))


class RamGuardWarnTest(RamGuardTestCase):
    def test_warn_starts_but_flags_lane(self):
        """warn + low free + huge lane -> starts, message carries WARNING."""
        self.set_mode("warn")
        self.set_memory(_LOW)
        lane, marker = self.stub_lane()
        self.assertFalse(os.path.exists(marker))

        ok, msg = core.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertIn("WARNING", msg)
        self.assertIn("starting", msg)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(core.script_lane_on(lane))

    def test_warn_clean_message_with_ample_free(self):
        self.set_mode("warn")
        self.set_memory(_AMPLE)
        lane, _ = self.stub_lane(approx_mb=2000)
        ok, msg = core.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertNotIn("refused", msg)
        self.assertNotIn("WARNING", msg)
        self.assertEqual(msg, "guard-lane starting (loads in background)")

    def test_block_clean_message_with_ample_free(self):
        """A clean start under block is indistinguishable from warn."""
        self.set_mode("block")
        self.set_memory(_AMPLE)
        lane, _ = self.stub_lane(approx_mb=2000)
        ok, msg = core.toggle(lane["id"], True)
        self.assertTrue(ok)
        self.assertNotIn("refused", msg)
        self.assertNotIn("WARNING", msg)
        self.assertEqual(msg, "guard-lane starting (loads in background)")


class RamGuardNoMemoryDataTest(RamGuardTestCase):
    def test_proceeds_when_ramtoggle_unavailable(self):
        """No free_mb -> no projection -> no blocking, even in block mode."""
        for mode in ("warn", "block"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.set_memory(_NO_MEMORY)
                name = f"nomem-{mode}"
                lane, marker = self.stub_lane(name=name, port=8800 + len(name),
                                              approx_mb=99999)
                self.assertFalse(os.path.exists(marker))
                ok, msg = core.toggle(lane["id"], True)
                self.assertTrue(ok)
                self.assertNotIn("refused", msg)
                self.assertNotIn("WARNING", msg)
                self.assertTrue(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
