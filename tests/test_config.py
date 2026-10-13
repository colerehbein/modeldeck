"""Tests for server/config.py — loading, deep merge, ~ expansion, validation."""
import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

import config as config_mod


class ConfigLoadTestCase(unittest.TestCase):
    """Base: every test runs inside its own temp dir with config paths patched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        helpers.restore_config()
        self._tmp.cleanup()


class LoadWithoutLocalTest(ConfigLoadTestCase):
    def test_example_only_load(self):
        """No config.local.json -> load() still returns the required keys."""
        # Point at an empty temp config dir (LOCAL does not exist there);
        # EXAMPLE stays the repo's committed example config.
        config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(config_dir)
        local_path = os.path.join(config_dir, "config.local.json")
        self.assertFalse(os.path.exists(local_path))
        helpers.patch_config_paths(config_dir, local_path)

        cfg = config_mod.load()

        self.assertIn("host", cfg["server"])
        self.assertIn("port", cfg["server"])
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")
        self.assertEqual(cfg["server"]["port"], 8790)
        self.assertIsInstance(cfg["lanes"], list)
        self.assertGreater(len(cfg["lanes"]), 0)
        for lane in cfg["lanes"]:
            self.assertIn("id", lane)
            self.assertTrue(lane["script"])


class DeepMergeTest(ConfigLoadTestCase):
    def test_partial_local_override_keeps_siblings(self):
        """A local config setting only server.port keeps example's server.host."""
        helpers.write_config(self.tmpdir, [], extra={"server": {"port": 1234}})
        # write_config always writes host 127.0.0.1; overwrite with a truly
        # partial override to prove the merge, not the helper, is at work.
        local_path = os.path.join(self.tmpdir, "config", "config.local.json")
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump({"server": {"port": 1234}}, f)

        cfg = config_mod.load()

        self.assertEqual(cfg["server"]["port"], 1234)
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")  # from example
        # Sibling top-level sections survive the overlay too.
        self.assertIn("lanes", cfg)
        self.assertIn("web", cfg)


class ExpandTest(ConfigLoadTestCase):
    def test_expand_paths_only(self):
        """_expand expands ~ in path keys, leaves display strings alone."""
        result = config_mod._expand({
            "ramtoggle": "~/bin/ramtoggle",
            "lanes": [
                {"label": "A ~ squiggle ~ lane", "script": "~/lanes/x.sh",
                 "impact": "~ tilde stays"},
            ],
        })
        home = os.path.expanduser("~")
        self.assertEqual(result["ramtoggle"], os.path.join(home, "bin/ramtoggle"))
        self.assertEqual(result["lanes"][0]["script"],
                         os.path.join(home, "lanes/x.sh"))
        self.assertEqual(result["lanes"][0]["label"], "A ~ squiggle ~ lane")
        self.assertEqual(result["lanes"][0]["impact"], "~ tilde stays")

    def test_load_expands_ramtoggle_but_not_labels(self):
        script = helpers.make_lane_script(self.tmpdir, 8801, "tilde-lane.sh")
        stub_rt = os.path.join(self.tmpdir, "rt-stub")
        with open(stub_rt, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub_rt, 0o755)
        # A literal "~" that only expanduser can resolve: nest under $HOME.
        rel = os.path.relpath(stub_rt, os.path.expanduser("~"))
        helpers.write_config(self.tmpdir, [
            {"id": "tilde", "label": "~not a path~", "kind": "script",
             "port": 8801, "script": script},
        ], ramtoggle="~/" + rel)

        cfg = config_mod.load()

        self.assertEqual(os.path.realpath(cfg["ramtoggle"]),
                         os.path.realpath(stub_rt))
        self.assertEqual(cfg["lanes"][0]["label"], "~not a path~")


class ValidateTest(ConfigLoadTestCase):
    def test_missing_host_raises_systemexit(self):
        with self.assertRaises(SystemExit) as ctx:
            config_mod._validate({"server": {"port": 8790}, "lanes": []})
        self.assertIn("server.host", str(ctx.exception))

    def test_missing_port_raises_systemexit(self):
        with self.assertRaises(SystemExit):
            config_mod._validate({"server": {"host": "127.0.0.1"}, "lanes": []})

    def test_lane_without_script_raises_systemexit(self):
        with self.assertRaises(SystemExit) as ctx:
            config_mod._validate({"server": {"host": "h", "port": 1},
                                  "lanes": [{"id": "x"}]})
        self.assertIn("script", str(ctx.exception))

    def test_load_with_host_missing_exits(self):
        """The same failure surfaces through the public load() entry point."""
        helpers.write_config(self.tmpdir, [], write_local=False)
        # Write a stripped example (no server.host) in place of the committed
        # one, with no local overlay to merge host back in.
        example_path = os.path.join(self.tmpdir, "config", "config.example.json")
        with open(example_path, "w", encoding="utf-8") as f:
            json.dump({"server": {"port": 8790}, "lanes": []}, f)
        orig_example, orig_local = config_mod.EXAMPLE, config_mod.LOCAL
        config_mod.EXAMPLE = example_path
        config_mod.LOCAL = Path(os.path.join(self.tmpdir, "config", "absent.json"))
        try:
            with self.assertRaises(SystemExit):
                config_mod.load()
        finally:
            config_mod.EXAMPLE, config_mod.LOCAL = orig_example, orig_local


class MinimalLaneTest(ConfigLoadTestCase):
    def test_minimal_lane_gets_display_defaults(self):
        """{id, port, script} loads with label==id, impact=='' fallbacks."""
        script = helpers.make_lane_script(self.tmpdir, 8802, "mini-lane.sh")
        helpers.write_config(self.tmpdir, [
            {"id": "mini", "port": 8802, "script": script},
        ])

        cfg = config_mod.load()

        lane = next(l for l in cfg["lanes"] if l["id"] == "mini")
        self.assertEqual(lane["label"], "mini")
        self.assertEqual(lane["impact"], "")
        self.assertIsNone(lane["approx_mb"])
        self.assertEqual(lane["port"], 8802)
        self.assertTrue(os.path.isabs(lane["script"]))


class ResolveRepoRelativeTest(ConfigLoadTestCase):
    def test_relative_script_resolved_against_repo_root(self):
        helpers.write_config(self.tmpdir, [
            {"id": "rel", "port": 8803, "script": "lanes/uzu-9b-lane.sh"},
        ])

        cfg = config_mod.load()

        lane = next(l for l in cfg["lanes"] if l["id"] == "rel")
        self.assertTrue(os.path.isabs(lane["script"]))
        self.assertTrue(lane["script"].endswith("lanes/uzu-9b-lane.sh"))
        self.assertTrue(os.path.exists(lane["script"]))


class WebPathTest(ConfigLoadTestCase):
    def test_web_path_resolves_relative_and_absolute(self):
        cfg = {"web": {"panel": "web/panel.html"}}
        self.assertEqual(
            config_mod.web_path(cfg, "panel"),
            helpers.REPO_ROOT / "web" / "panel.html")
        abs_path = os.path.join(self.tmpdir, "abs.html")
        self.assertEqual(
            str(config_mod.web_path({"web": {"panel": abs_path}}, "panel")),
            abs_path)
        self.assertIsNone(config_mod.web_path({}, "panel"))


if __name__ == "__main__":
    unittest.main()
