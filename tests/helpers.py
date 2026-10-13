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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import config as config_mod  # noqa: E402  (needs the sys.path insert above)


_LANE_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Test lane stub: start/stop flip a marker file; status reports it.
# start HOLDS like the real lane scripts (which exec a long-running server
# and never return): it creates the marker, then sleeps STUB_HOLD seconds
# (default 2, overridable) so thrash_server.toggle(start) sees a live
# process (poll() is None) instead of a false "failed to start".
set -euo pipefail
MARKER="{marker}"
PORT="{port}"
case "${{1:-}}" in
  start) touch "$MARKER"; sleep "${{STUB_HOLD:-{hold}}}";;
  stop) rm -f "$MARKER"; pkill -f "$0 start" >/dev/null 2>&1 || true; echo "stopped";;
  status) if [ -f "$MARKER" ]; then echo "UP on :${{PORT}}"; else echo "DOWN"; fi;;
  *) echo "usage: {name} {{start|stop|status}}" >&2; exit 1;;
esac
"""

_RAMTOGGLE_STUB = """#!/usr/bin/env bash
# Stateful ramtoggle stub: --json reads {state}; on/off write it, so the
# round trip toggle(id, on) -> --json {{"on": true}} is observable in tests.
set -euo pipefail
STATE="{state}"
case "${{1:-}}" in
  --json)
    if [ -f "$STATE" ]; then ON=$(cat "$STATE"); else ON=false; fi
    printf '%s' '{{"memory":{{"total_mb":32768,"available_mb":20000,"free_mb":18000,"wired_mb":7000,"compressed_mb":2000}},"lanes":[{{"id":"stub-lane","label":"Stub","port":12399,"on":'$ON',"approx_mb":1000,"kind":"ramtoggle","impact":""}}]}}'
  ;;
  on|off)
    if [ "$1" = on ]; then echo true > "$STATE"; else echo false > "$STATE"; fi
    echo "toggled ${{2:-stub-lane}} $1"
  ;;
  *) echo "usage: ramtoggle --json|on|off <id>" >&2; exit 1;;
esac
"""

# Saved originals of the config module globals patched by write_config().
_patched = {}


def write_exec(path, body):
    """Write body to path, chmod it executable, return the path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def make_lane_script(directory, port, name, hold=2):
    """Write an executable lane stub supporting start/stop/status.

    start  -> touch directory/<name>.up, then hold for `hold` seconds
              (STUB_HOLD env overrides) before exiting — models the real
              lane scripts, which exec a blocking server and never return
    stop   -> remove the marker (and kill any lingering start process)
    status -> 'UP on :<port>' when the marker exists, else 'DOWN'
    Returns the script path.
    """
    directory = os.fspath(directory)
    marker = os.path.join(directory, name + ".up")
    script = os.path.join(directory, name)
    return write_exec(script, _LANE_SCRIPT_TEMPLATE.format(
        marker=marker, port=port, name=name, hold=hold))


def make_ramtoggle_stub(directory):
    """Write a stateful executable ramtoggle stub (--json / on / off).

    State lives in <directory>/rt.state. Returns the script path.
    """
    directory = os.fspath(directory)
    state = os.path.join(directory, "rt.state")
    return write_exec(
        os.path.join(directory, "ramtoggle-stub"),
        _RAMTOGGLE_STUB.format(state=state))


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
