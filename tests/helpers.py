"""Shared helpers for the Thrash test suite (stdlib only).

Every test builds its own temp directory — nothing here ever touches the
repo's real config/config.local.json.

Import this module BEFORE importing `config` directly: it puts server/ on
sys.path so `import config` resolves to server/config.py, not the repo's
config/ directory.
"""
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import config as config_mod  # noqa: E402  (needs the sys.path insert above)


_LANE_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Test lane stub: start/stop flip a marker file; status reports it.
set -euo pipefail
MARKER="{marker}"
PORT="{port}"
case "${{1:-}}" in
  start) touch "$MARKER"; echo "started";;
  stop) rm -f "$MARKER"; echo "stopped";;
  status) if [ -f "$MARKER" ]; then echo "UP on :${{PORT}}"; else echo "DOWN"; fi;;
  *) echo "usage: {name} {{start|stop|status}}" >&2; exit 1;;
esac
"""

_RAMTOGGLE_STUB = """#!/usr/bin/env bash
# Test ramtoggle stub: --json reports one fake lane; on/off echo the action.
case "${{1:-}}" in
  --json) cat <<'JSON'
{memory_json}
JSON
  ;;
  on|off) echo "toggled ${{2:-}} ${{1}}";;
  *) echo "usage: ramtoggle --json|on|off <id>" >&2; exit 1;;
esac
"""

_RAMTOGGLE_MEMORY_JSON = (
    '{"memory":{"total_mb":32768,"available_mb":20000,"free_mb":18000,'
    '"wired_mb":7000,"compressed_mb":2000},"lanes":[{"id":"stub-lane",'
    '"label":"Stub","port":12399,"on":false,"approx_mb":1000,'
    '"kind":"ramtoggle","impact":""}]}'
)

# Saved originals of the config module globals patched by write_config().
_patched = {}


def _write_exec(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def make_lane_script(directory, port, name):
    """Write an executable lane stub supporting start/stop/status.

    start  -> touch directory/<name>.up and exit
    stop   -> remove the marker
    status -> 'UP on :<port>' when the marker exists, else 'DOWN'
    Returns the script path.
    """
    directory = os.fspath(directory)
    marker = os.path.join(directory, name + ".up")
    script = os.path.join(directory, name)
    return _write_exec(script, _LANE_SCRIPT_TEMPLATE.format(
        marker=marker, port=port, name=name))


def make_ramtoggle_stub(directory):
    """Write an executable ramtoggle stub (--json / on / off). Returns path."""
    directory = os.fspath(directory)
    return _write_exec(
        os.path.join(directory, "ramtoggle-stub"),
        _RAMTOGGLE_STUB.format(memory_json=_RAMTOGGLE_MEMORY_JSON))


def write_config(directory, lanes, ramtoggle=None, extra=None, write_local=True):
    """Point config_mod at a temp config dir and write a config.local.json.

    Makes directory/config/, writes config.local.json (server host/port,
    optional ramtoggle, the given lanes, plus any `extra` top-level keys),
    then patches config_mod.CONFIG_DIR / config_mod.LOCAL so config.load()
    reads the stub. Call restore_config() (e.g. in tearDown) afterwards.

    Pass write_local=False to leave the local file absent (simulates a
    machine with only the committed example config).
    """
    config_dir = os.path.join(os.fspath(directory), "config")
    os.makedirs(config_dir, exist_ok=True)
    local = {"server": {"host": "127.0.0.1", "port": 8790}}
    if ramtoggle is not None:
        local["ramtoggle"] = ramtoggle
    if extra:
        local.update(extra)
    local["lanes"] = lanes
    local_path = os.path.join(config_dir, "config.local.json")
    if write_local:
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(local, f)
    patch_config_paths(config_dir, local_path)
    return config_dir


def patch_config_paths(config_dir, local_path):
    """Repoint config_mod.CONFIG_DIR/LOCAL (originals kept for restore)."""
    _patched.setdefault("CONFIG_DIR", config_mod.CONFIG_DIR)
    _patched.setdefault("LOCAL", config_mod.LOCAL)
    config_mod.CONFIG_DIR = Path(config_dir)
    config_mod.LOCAL = Path(local_path)


def restore_config():
    """Undo every patch_config_paths()/write_config() in this process."""
    if "CONFIG_DIR" in _patched:
        config_mod.CONFIG_DIR = _patched.pop("CONFIG_DIR")
        config_mod.LOCAL = _patched.pop("LOCAL")


@contextmanager
def config_at(config_dir, local_path):
    """Context manager form of patch_config_paths for one-off patches."""
    orig_cd, orig_local = config_mod.CONFIG_DIR, config_mod.LOCAL
    config_mod.CONFIG_DIR = Path(config_dir)
    config_mod.LOCAL = Path(local_path)
    try:
        yield config_dir
    finally:
        config_mod.CONFIG_DIR = orig_cd
        config_mod.LOCAL = orig_local
