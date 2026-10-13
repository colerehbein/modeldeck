"""Tests for the setup diagnostics in server/doctor.py.

`import doctor` must never see the machine's real config: doctor reads
config.load() and core's globals, so this module imports doctor against a
throwaway empty config (the same isolation pattern tests/test_reaper.py
uses) and restores afterwards.

Everything is asserted through run_checks() — main() is only a printer over
it, so these tests never capture stdout. Per test, core.SCRIPT_LANES /
core.RAMTOGGLE / core.ramtoggle_status are stubbed and restored in tearDown,
and the checks that touch ~/Library/LaunchAgents are redirected at a temp
dir by monkeypatching doctor._LAUNCHD_AGENTS so no assertion depends on what
happens to be installed on the host.
"""
import os
import tempfile
import unittest

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

# Import doctor against a throwaway config so its import-time work never
# touches the machine's real config.local.json.
_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import doctor  # noqa: E402

helpers.restore_config()


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


def use_real_config():
    """Un-patch the config paths for a check that must read the real config.

    doctor's `config_mod` is the same object helpers patches, so the
    throwaway paths written at import time are already restored to the
    machine's real config/ by tearDownModule-time restore_config(); this
    just makes the intent explicit at the call site and keeps the config
    check tests honest about reading config.example.json +
    config.local.json from the repo.
    """
    helpers.restore_config()


def results_as_dict(results):
    return {name: (ok, detail) for name, ok, detail in results}


def detail_of(results, name):
    return results_as_dict(results)[name][1]


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_lanes = doctor.core.SCRIPT_LANES
        self._orig_ramtoggle = doctor.core.RAMTOGGLE
        self._orig_rt_status = doctor.core.ramtoggle_status
        self._orig_agents = doctor._LAUNCHD_AGENTS
        self._orig_llama = doctor._LLAMA_SERVER
        self._orig_uzu = doctor._UZU_CLI
        # Redirect the launchd check at an empty temp dir: the host may or
        # may not have the agents installed, and that must not decide a test.
        doctor._LAUNCHD_AGENTS = ()
        self._orig_expanduser = doctor.os.path.expanduser

        def fake_expanduser(path):
            if path in ("~/Library/LaunchAgents",):
                return os.path.join(self.tmpdir, "LaunchAgents")
            return self._orig_expanduser(path)

        doctor.os.path.expanduser = fake_expanduser

    def tearDown(self):
        doctor.core.SCRIPT_LANES = self._orig_lanes
        doctor.core.RAMTOGGLE = self._orig_ramtoggle
        doctor.core.ramtoggle_status = self._orig_rt_status
        doctor._LAUNCHD_AGENTS = self._orig_agents
        doctor._LLAMA_SERVER = self._orig_llama
        doctor._UZU_CLI = self._orig_uzu
        doctor.os.path.expanduser = self._orig_expanduser
        self._tmp.cleanup()

    def stub_lane(self, name, port, script=None):
        """Register one script lane; default to a real executable stub."""
        if script is None:
            script = helpers.make_lane_script(self.tmpdir, port, name + ".sh")
        lane = {"id": name, "label": name, "kind": "script", "port": port,
                "approx_mb": 1000, "impact": "", "script": script}
        doctor.core.SCRIPT_LANES = dict(doctor.core.SCRIPT_LANES)
        doctor.core.SCRIPT_LANES[name] = lane
        return lane

    def no_ramtoggle(self):
        """No ramtoggle configured and no ramtoggle lanes."""
        doctor.core.RAMTOGGLE = None
        doctor.core.ramtoggle_status = lambda: ([], {})


class ConfigCheckTests(DoctorTestCase):
    def test_config_check_passes_on_a_loadable_config(self):
        use_real_config()
        ok, detail = doctor.check_config()
        self.assertTrue(ok)
        self.assertIn("lane", detail)  # count varies by machine config

    def test_config_check_reports_failure(self):
        # A lane with no `id` trips config.load()'s documented validation and
        # raises SystemExit; doctor must catch it and report a failing check
        # instead of propagating (SystemExit would otherwise kill the CLI).
        import json
        config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(config_dir, exist_ok=True)
        local_path = os.path.join(config_dir, "config.local.json")
        with open(local_path, "w") as f:
            json.dump({"lanes": [{"script": "lanes/x.sh"}]}, f)
        helpers.patch_config_paths(config_dir, local_path)
        ok, detail = doctor.check_config()
        self.assertFalse(ok)
        self.assertIn("failed to load", detail)


class LaneScriptCheckTests(DoctorTestCase):
    def test_all_scripts_present_and_executable(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        self.stub_lane("lane-b", 8892)
        ok, detail = doctor.check_lane_scripts()
        self.assertTrue(ok, detail)
        self.assertIn("2 lane script", detail)

    def test_missing_script_fails(self):
        script = os.path.join(self.tmpdir, "gone.sh")  # never written
        self.stub_lane("lane-a", 8891, script=script)
        ok, detail = doctor.check_lane_scripts()
        self.assertFalse(ok)
        self.assertIn("gone.sh", detail)
        self.assertIn("missing", detail)

    def test_non_executable_script_fails(self):
        script = os.path.join(self.tmpdir, "notexec.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(script, 0o644)
        self.stub_lane("lane-a", 8891, script=script)
        ok, detail = doctor.check_lane_scripts()
        self.assertFalse(ok)
        self.assertIn("not executable", detail)


class PortCollisionCheckTests(DoctorTestCase):
    def test_distinct_ports_pass(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        self.stub_lane("lane-b", 8892)
        doctor.core.RAMTOGGLE = None
        doctor.core.ramtoggle_status = lambda: ([], {})
        ok, _ = doctor.check_port_collisions()
        self.assertTrue(ok)

    def test_collision_between_two_script_lanes_fails(self):
        self.stub_lane("lane-a", 8891)
        doctor.core.SCRIPT_LANES["lane-b"] = {
            "id": "lane-b", "port": 8891,
            "script": os.path.join(self.tmpdir, "unused.sh")}
        ok, detail = doctor.check_port_collisions()
        self.assertFalse(ok)
        self.assertIn("8891", detail)
        self.assertIn("lane-a", detail)
        self.assertIn("lane-b", detail)

    def test_collision_across_script_and_ramtoggle_lanes_fails(self):
        self.stub_lane("lane-a", 8891)
        doctor.core.ramtoggle_status = lambda: ([
            {"id": "rt-lane", "port": 8891, "on": False}], {})
        ok, detail = doctor.check_port_collisions()
        self.assertFalse(ok)
        self.assertIn("8891", detail)
        self.assertIn("rt-lane", detail)

    def test_none_port_is_skipped_not_collided(self):
        self.stub_lane("lane-a", None)
        self.stub_lane("lane-b", None)
        ok, _ = doctor.check_port_collisions()
        self.assertTrue(ok)


class RamtoggleCheckTests(DoctorTestCase):
    def test_not_configured_is_ok(self):
        doctor.core.RAMTOGGLE = None
        ok, detail = doctor.check_ramtoggle()
        self.assertTrue(ok)
        self.assertIn("not configured", detail)

    def test_configured_but_missing_fails(self):
        doctor.core.RAMTOGGLE = os.path.join(self.tmpdir, "nope", "ramtoggle")
        ok, detail = doctor.check_ramtoggle()
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_configured_and_present_passes(self):
        path = helpers.make_ramtoggle_stub(self.tmpdir)
        doctor.core.RAMTOGGLE = path
        ok, detail = doctor.check_ramtoggle()
        self.assertTrue(ok, detail)
        self.assertIn("executable", detail)


class BinariesCheckTests(DoctorTestCase):
    def test_missing_binaries_are_advisory_not_failures(self):
        # Force both advisory paths to a location that cannot exist, so the
        # assertion holds on any host regardless of what is installed.
        doctor._LLAMA_SERVER = os.path.join(self.tmpdir, "no-llama-server")
        doctor._UZU_CLI = os.path.join(self.tmpdir, "no-uzu-cli")
        ok, detail = doctor.check_binaries()
        self.assertTrue(ok, detail)
        self.assertIn("advisory", detail)

    def test_both_present_is_reported(self):
        doctor._LLAMA_SERVER = self.tmpdir  # a path that exists
        doctor._UZU_CLI = self.tmpdir
        ok, detail = doctor.check_binaries()
        self.assertTrue(ok)
        self.assertIn("both present", detail)


class RunChecksTests(DoctorTestCase):
    def test_all_good_returns_zero_failures(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        self.stub_lane("lane-b", 8892)
        self.no_ramtoggle()
        results = doctor.run_checks()
        self.assertEqual(len(results), len(doctor.CHECKS))
        by_name = results_as_dict(results)
        for name, (ok, _) in by_name.items():
            self.assertTrue(ok, f"{name} unexpectedly failed")
        # The two advisory checks must never carry a failure, whatever the
        # host machine has installed.
        self.assertTrue(by_name["binaries"][0])
        self.assertTrue(by_name["launchd agents"][0])

    def test_missing_script_makes_run_checks_fail(self):
        doctor.core.SCRIPT_LANES = {}
        script = os.path.join(self.tmpdir, "gone.sh")
        self.stub_lane("lane-a", 8891, script=script)
        self.no_ramtoggle()
        results = doctor.run_checks()
        self.assertFalse(results_as_dict(results)["lane scripts"][0])

    def test_port_collision_makes_run_checks_fail(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        doctor.core.SCRIPT_LANES["lane-b"] = {
            "id": "lane-b", "port": 8891, "script": "unused"}
        self.no_ramtoggle()
        results = doctor.run_checks()
        self.assertFalse(results_as_dict(results)["port collisions"][0])


class MainExitCodeTests(DoctorTestCase):
    def _capture(self):
        import contextlib
        import io
        return io.StringIO(), contextlib.redirect_stdout

    def test_main_returns_zero_when_all_checks_pass(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        self.no_ramtoggle()
        buf, redirect = self._capture()
        with redirect(buf):
            rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("passed", buf.getvalue())
        self.assertIn("✓", buf.getvalue())

    def test_main_returns_one_when_a_check_fails(self):
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891, script=os.path.join(
            self.tmpdir, "gone.sh"))
        self.no_ramtoggle()
        buf, redirect = self._capture()
        with redirect(buf):
            rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("✗", buf.getvalue())
        self.assertIn("failed", buf.getvalue())

    def test_missing_binaries_do_not_fail_the_run(self):
        # A machine with neither llama-server nor the uzu CLI installed must
        # still get a green doctor — the binaries check is advisory.
        doctor.core.SCRIPT_LANES = {}
        self.stub_lane("lane-a", 8891)
        self.no_ramtoggle()
        # Point the advisory paths somewhere that cannot exist.
        doctor._LLAMA_SERVER = os.path.join(self.tmpdir, "no-llama-server")
        doctor._UZU_CLI = os.path.join(self.tmpdir, "no-uzu-cli")
        buf, redirect = self._capture()
        with redirect(buf):
            rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("advisory", buf.getvalue())
        self.assertNotIn("✗", buf.getvalue())

    def test_run_is_an_alias_of_main(self):
        self.assertIs(doctor.run, doctor.main)


if __name__ == "__main__":
    unittest.main()
