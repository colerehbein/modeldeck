"""
doctor.py — environment/setup diagnostics for Thrash.

Read-only and fast by design: it inspects the config and the filesystem and
never starts a lane, loads a model or opens a socket, so `thrash doctor` is
safe to run on a machine that is supposed to be idle. Its job is to answer
"would a lane actually start if I asked for it?" — config parses, every lane
script is present and executable, no two lanes claim the same port, the
external binaries the lanes shell out to exist, and the launchd agents that
keep the panel and menubar alive are installed.

Two kinds of result share one shape, `(ok, detail)`:

  - a real failure (missing/unexecutable script, port collision, unloadable
    config) fails the run, so `main()` exits 1;
  - a soft observation (a binary this machine simply does not have) is
    reported but always `ok=True`, so doctor stays green on a laptop without
    the uzu toolchain or on a fresh checkout with nothing installed yet.

main() is a printer over run_checks(); every assertion in the tests goes
through run_checks(), which returns [(name, ok, detail), ...] and performs
no I/O of its own. Like the rest of the server modules it is imported as a
top-level `doctor` (server/ is on sys.path), and reads core's globals, which
the tests patch directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402  (needs the sys.path insert above)
import config as config_mod  # noqa: E402

# Binaries the lane scripts shell out to. Advisory only: a machine that runs
# the llama.cpp lanes does not need the uzu CLI, and vice versa, so a missing
# entry is noted rather than failed.
_LLAMA_SERVER = "/opt/homebrew/bin/llama-server"
_UZU_CLI = os.path.join(os.path.expanduser("~"), "projects", "uzu",
                        "target", "release", "cli")

# launchd agents installed by docs/launchd.md. Absence on a dev box (nothing
# installed yet, or the agents were only ever loaded ad hoc) is not a defect,
# so these are reported as ok either way — the value here is telling the
# operator which of the two are installed.
_LAUNCHD_AGENTS = ("com.cole.thrash.plist", "com.cole.thrash-menubar.plist")

MARK_OK, MARK_FAIL = "✓", "✗"


def check_config():
    """config.load() succeeds and reports the lane count."""
    try:
        cfg = config_mod.load()
    except SystemExit as e:
        return False, f"config failed to load: {e}"
    except Exception as e:  # noqa: BLE001 — any loader crash is a doctor fail
        return False, f"config failed to load: {type(e).__name__}: {e}"
    lanes = len(cfg.get("lanes") or [])
    ramtoggle = bool(cfg.get("ramtoggle"))
    source = "ramtoggle + script lanes" if ramtoggle else "script lanes only"
    return True, f"loaded {lanes} lane(s), {source}"


def check_lane_scripts():
    """Every configured lane script exists and is executable."""
    bad = []
    checked = 0
    for lane in core.SCRIPT_LANES.values():
        script = lane.get("script")
        if not script:
            continue
        checked += 1
        path = os.path.expanduser(script)
        if not os.path.exists(path):
            bad.append(f"{lane.get('id', '?')}: missing {script}")
        elif not os.access(path, os.X_OK):
            bad.append(f"{lane.get('id', '?')}: not executable {script}")
    if bad:
        return False, f"{len(bad)} of {checked} script(s) unusable: " + "; ".join(bad)
    return True, f"all {checked} lane script(s) present and executable"


def check_port_collisions():
    """No two lanes (script or ramtoggle) claim the same port."""
    ports = {}
    for lane in core.SCRIPT_LANES.values():
        port = lane.get("port")
        if port is None:
            continue
        ports.setdefault(port, []).append(lane.get("id", "?"))
    try:
        rt_lanes, _ = core.ramtoggle_status()
    except Exception:  # noqa: BLE001 — an unreachable ramtoggle is its own problem
        rt_lanes = []
    for lane in rt_lanes:
        port = lane.get("port")
        if port is None:
            continue
        ports.setdefault(port, []).append(lane.get("id", "?"))
    collisions = {p: ids for p, ids in ports.items() if len(ids) > 1}
    if collisions:
        detail = ", ".join(
            f"port {p} claimed by {', '.join(sorted(ids))}"
            for p, ids in sorted(collisions.items()))
        return False, detail
    return True, f"no collisions across {len(ports)} distinct port(s)"


def check_ramtoggle():
    """The configured ramtoggle path exists (when one is configured)."""
    path = core.RAMTOGGLE
    if not path:
        return True, "ramtoggle not configured (script lanes only)"
    resolved = os.path.expanduser(path)
    if not os.path.exists(resolved):
        return False, f"ramtoggle configured but not found: {resolved}"
    if not os.access(resolved, os.X_OK):
        return False, f"ramtoggle found but not executable: {resolved}"
    return True, f"ramtoggle present and executable at {resolved}"


def check_launchd():
    """Best-effort: are the Thrash launchd agents installed?"""
    agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    if not os.path.isdir(agents_dir):
        return True, f"launchd not checked (no {agents_dir} — dev machine)"
    present = [a for a in _LAUNCHD_AGENTS
               if os.path.exists(os.path.join(agents_dir, a))]
    missing = [a for a in _LAUNCHD_AGENTS if a not in present]
    detail = f"{len(present)}/{len(_LAUNCHD_AGENTS)} installed"
    if missing:
        detail += f" (missing: {', '.join(missing)})"
    else:
        detail += f" ({', '.join(present)})"
    # Deliberately ok either way: these agents keep the panel/menubar alive,
    # not the lanes, and `launchctl load` is easy to redo. Advisory.
    return True, detail


def check_binaries():
    """Advisory: llama-server and the uzu CLI are the binaries lanes invoke.

    Never fails the run — a machine legitimately runs one lane family and not
    the other — but names what is missing so the operator sees it.
    """
    named = (("llama-server", _LLAMA_SERVER), ("uzu cli", _UZU_CLI))
    present = [label for label, path in named if os.path.exists(path)]
    missing = [label for label, path in named if not os.path.exists(path)]
    if not missing:
        return True, "llama-server and uzu cli both present"
    detail = f"present: {', '.join(present) or 'none'}"
    if missing:
        detail += f"; missing (advisory): {', '.join(missing)}"
    return True, detail


# (display name, check fn -> (ok, detail)). Order is the report order.
CHECKS = [
    ("config", check_config),
    ("lane scripts", check_lane_scripts),
    ("port collisions", check_port_collisions),
    ("ramtoggle", check_ramtoggle),
    ("launchd agents", check_launchd),
    ("binaries", check_binaries),
]


def run_checks():
    """Run every check; return [(name, ok, detail), ...]."""
    return [(name, *fn()) for name, fn in CHECKS]


def _report_line(mark, name, detail):
    return f"{mark} {name}: {detail}"


def main(argv=None):
    """Print the doctor report; return 0 if every hard check passed, else 1."""
    results = run_checks()
    for name, ok, detail in results:
        print(_report_line(MARK_OK if ok else MARK_FAIL, name, detail))
    failures = [(name, detail) for name, ok, detail in results if not ok]
    if failures:
        print(f"\n{len(failures)} of {len(results)} check(s) failed:")
        for name, detail in failures:
            print(_report_line(MARK_FAIL, name, detail))
        return 1
    print(f"\nall {len(results)} check(s) passed")
    return 0


run = main  # the CLI accepts either entry point


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
