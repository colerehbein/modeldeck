"""CLI tests — run `thrash` as a subprocess against a stub config.

The CLI is a separate process, so it cannot see in-process monkeypatches.
Instead each test writes a temp config.local.json (stub lane script + stub
ramtoggle, via tests/helpers.py) and points the CLI at it with the
THRASH_CONFIG_LOCAL environment variable the CLI honors.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRASH = os.path.join(REPO_ROOT, "thrash")

sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
import helpers  # noqa: E402


class CliTestCase(unittest.TestCase):
    """Base: temp dir + stub config, THRASH_CONFIG_LOCAL wired for the child."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="thrash-cli-")
        self.lane_script = helpers.make_lane_script(self.tmp, 8799, "cli-stub", hold=2)
        self.marker = os.path.join(self.tmp, "cli-stub.up")
        self.ramtoggle = helpers.make_ramtoggle_stub(self.tmp)
        self.config_dir = helpers.write_config(
            self.tmp,
            lanes=[{
                "id": "cli-stub",
                "label": "Stub Lane",
                "kind": "script",
                "port": 8799,
                "approx_mb": 4096,
                "impact": "test lane",
                "script": self.lane_script,
                "log": os.path.join(self.tmp, "cli-stub.log"),
            }],
            ramtoggle=self.ramtoggle,
        )
        self.config_local = os.path.join(self.config_dir, "config.local.json")
        self._old_env = os.environ.get("THRASH_CONFIG_LOCAL")
        os.environ["THRASH_CONFIG_LOCAL"] = self.config_local

    def tearDown(self):
        helpers.restore_config()
        if self._old_env is None:
            os.environ.pop("THRASH_CONFIG_LOCAL", None)
        else:
            os.environ["THRASH_CONFIG_LOCAL"] = self._old_env
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, timeout=30):
        env = dict(os.environ, THRASH_CONFIG_LOCAL=self.config_local, STUB_HOLD="1")
        p = subprocess.run(
            [sys.executable, THRASH] + list(args),
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return p


class TestStatus(CliTestCase):
    def test_status_table_lists_stub_lane_and_memory(self):
        p = self.run_cli("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("cli-stub", p.stdout)
        self.assertIn("memory:", p.stdout)
        self.assertIn("\u25cb", p.stdout)  # lane starts OFF

    def test_status_json_parses_and_lists_stub_lane(self):
        p = self.run_cli("status", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertIn("memory", data)
        ids = [l["id"] for l in data["lanes"]]
        self.assertIn("cli-stub", ids)
        lane = next(l for l in data["lanes"] if l["id"] == "cli-stub")
        self.assertEqual(lane["port"], 8799)
        self.assertEqual(lane["ram_mb"], 4096)
        self.assertFalse(lane["on"])

    def test_status_json_includes_ramtoggle_lanes_and_memory(self):
        data = json.loads(self.run_cli("status", "--json").stdout)
        kinds = {l["kind"] for l in data["lanes"]}
        self.assertIn("script", kinds)
        self.assertIn("ramtoggle", kinds)  # the stub ramtoggle contributes lanes
        self.assertEqual(data["memory"].get("total_mb"), 32768)


class TestOnOff(CliTestCase):
    def test_on_creates_marker_and_exits_zero(self):
        p = self.run_cli("on", "cli-stub", timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(os.path.exists(self.marker))

    def test_off_removes_marker(self):
        self.assertEqual(self.run_cli("on", "cli-stub", timeout=20).returncode, 0)
        p = self.run_cli("off", "cli-stub")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse(os.path.exists(self.marker))

    def test_on_unknown_lane_exits_one(self):
        p = self.run_cli("on", "no-such-lane")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown lane", p.stdout + p.stderr)

    def test_off_unknown_lane_exits_one(self):
        p = self.run_cli("off", "no-such-lane")
        self.assertEqual(p.returncode, 1)

    def test_on_without_id_exits_two(self):
        p = self.run_cli("on")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", (p.stdout + p.stderr).lower())


class TestToggle(CliTestCase):
    def test_toggle_flips_state(self):
        self.assertEqual(self.run_cli("on", "cli-stub", timeout=20).returncode, 0)
        p = self.run_cli("toggle", "cli-stub")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(self.marker))
        p = self.run_cli("toggle", "cli-stub", timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(self.marker))

    def test_toggle_unknown_lane_exits_one(self):
        p = self.run_cli("toggle", "no-such-lane")
        self.assertEqual(p.returncode, 1)

    def test_toggle_without_id_exits_two(self):
        p = self.run_cli("toggle")
        self.assertEqual(p.returncode, 2)


class TestLogs(CliTestCase):
    def test_logs_known_lane_degrades_gracefully(self):
        p = self.run_cli("logs", "cli-stub")
        self.assertEqual(p.returncode, 0, p.stderr)
        # either core.read_log_tail exists (no log yet) or the fallback hint
        self.assertIn("log", (p.stdout + p.stderr).lower())

    def test_logs_unknown_lane_exits_one(self):
        p = self.run_cli("logs", "no-such-lane")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown lane", p.stdout + p.stderr)

    def test_logs_without_id_exits_two(self):
        p = self.run_cli("logs")
        self.assertEqual(p.returncode, 2)

    def test_logs_accepts_lines_flag(self):
        p = self.run_cli("logs", "cli-stub", "--lines", "10")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_logs_bad_lines_flag_exits_two(self):
        p = self.run_cli("logs", "cli-stub", "--lines", "abc")
        self.assertEqual(p.returncode, 2)


class TestDoctor(CliTestCase):
    def test_doctor_does_not_crash(self):
        p = self.run_cli("doctor")
        self.assertIn(p.returncode, (0, 1))
        self.assertTrue(p.stdout.strip())


class TestUsage(CliTestCase):
    def test_no_args_exits_two_with_usage(self):
        p = self.run_cli()
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", (p.stdout + p.stderr).lower())

    def test_unknown_command_exits_two(self):
        p = self.run_cli("bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown command", p.stderr)

    def test_config_local_env_is_honored(self):
        # sanity: the stub config actually reaches the CLI
        p = self.run_cli("status", "--json")
        data = json.loads(p.stdout)
        self.assertEqual(
            next(l for l in data["lanes"] if l["id"] == "cli-stub")["port"], 8799)


if __name__ == "__main__":
    unittest.main()
