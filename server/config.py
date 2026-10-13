"""
config.py — Thrash configuration loader.

Loads config/config.example.json as the base, then overlays
config/config.local.json (gitignored) if present, via a recursive merge so a
partial local override cannot delete sibling keys. Expands `~` in path fields
only (never in labels/impact, which may legitimately contain `~`) and resolves
repo-relative script/web paths against the repo root.

Every machine-specific value (ports, script paths, model dirs) lives in
config so the code stays generic and reusable.
"""
import copy
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
EXAMPLE = CONFIG_DIR / "config.example.json"
LOCAL = CONFIG_DIR / "config.local.json"

# Keys whose string values are filesystem paths (safe to expanduser + resolve).
_PATH_KEYS = {"ramtoggle", "script", "log"}


def _deep_merge(base, overlay):
    """Recursively merge overlay into base; dicts merge, everything else replaces."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _expand(value, key=None):
    """Expand `~` only in values that are actually paths (never in display text)."""
    if isinstance(value, str):
        if key in _PATH_KEYS:
            return os.path.expanduser(value)
        return value
    if isinstance(value, dict):
        return {k: _expand(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _resolve_repo_relative(cfg):
    """Script/log paths in lane defs are repo-relative; make them absolute."""
    for lane in cfg.get("lanes", []) or []:
        for field in ("script", "log"):
            path = lane.get(field)
            if path and not os.path.isabs(os.path.expanduser(path)):
                lane[field] = str(REPO_ROOT / path)
    return cfg


def _validate(cfg):
    """Fail loudly at startup with a clear message on missing required keys."""
    missing = []
    server = cfg.get("server") or {}
    if "host" not in server:
        missing.append("server.host")
    if "port" not in server:
        missing.append("server.port")
    for i, lane in enumerate(cfg.get("lanes") or []):
        if not lane.get("id"):
            missing.append(f"lanes[{i}].id")
        if not lane.get("script"):
            missing.append(f"lanes[{i}].script (required for kind='script')")
    if missing:
        raise SystemExit(
            "config error — missing required keys: " + ", ".join(missing)
            + "\nEdit config/config.local.json (see config/config.example.json)."
        )


# Optional, defaulted feature blocks. Populated in load() so callers can read
# cfg["idle_unload"]["enabled"] etc. without guarding for absence, and so configs
# written before these keys existed keep working unchanged.
#
# Each value maps a sub-key to (default, validator, human_type). The validator
# returns True when the value is acceptable. Table-driven so the bool-subclass-
# of-int trap is handled identically for every key.
def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _pos_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _a_bool(v):
    return isinstance(v, bool)


def _a_list(v):
    return isinstance(v, list)


def _a_mode(v):
    return v in ("warn", "block")


_FEATURE_DEFAULTS = {
    "idle_unload": {
        "enabled": (False, _a_bool, "a boolean"),
        "ttl_minutes": (30, _pos_int, "an integer > 0"),
        "lanes": (["*"], _a_list, 'a list of lane ids (or ["*"])'),
    },
    "notifications": {
        "on_load": (True, _a_bool, "a boolean"),
        "on_low_memory": (True, _a_bool, "a boolean"),
        "low_memory_gb": (6, _pos_num, "a number > 0"),
    },
    "ram_guard": {
        "mode": ("warn", _a_mode, 'one of "warn" | "block"'),
    },
}


def _apply_feature_defaults(cfg):
    """Fill missing optional keys with defaults, then validate supplied values.

    Fills first so a partially-specified block is never rejected for a key the
    user did not set; only the final (defaulted-or-supplied) values are checked.
    Defaults are deep-copied so mutating a loaded config never poisons the
    module-level default for the next load().
    """
    for key, spec in _FEATURE_DEFAULTS.items():
        section = cfg.get(key)
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise SystemExit(
                f"config error — {key} must be an object, got "
                f"{type(section).__name__}"
            )
        for sub, (default, validator, human) in spec.items():
            if sub not in section:
                section[sub] = copy.deepcopy(default)
            if not validator(section[sub]):
                raise SystemExit(
                    f"config error — {key}.{sub} must be {human}, "
                    f"got {section[sub]!r}"
                )
        cfg[key] = section
    return cfg


def load():
    with open(EXAMPLE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if LOCAL.exists():
        with open(LOCAL, "r", encoding="utf-8") as f:
            local = json.load(f)
        _deep_merge(cfg, local)
    cfg = _expand(cfg)
    cfg = _resolve_repo_relative(cfg)
    _validate(cfg)
    # label/impact are display strings; provide safe fallbacks so a minimal
    # lane entry (id + port + script) still works, per the README.
    for lane in cfg.get("lanes") or []:
        lane.setdefault("label", lane["id"])
        lane.setdefault("impact", "")
        lane.setdefault("approx_mb", None)
        log = lane.get("log")
        if log is not None and not isinstance(log, str):
            raise SystemExit(
                f"config error — lanes[{lane.get('id', '?')}].log must be a "
                f"string path, got {type(log).__name__}"
            )
    # Optional feature blocks: fill defaults, then validate supplied values.
    _apply_feature_defaults(cfg)
    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def web_path(cfg, key):
    """Absolute path to a web asset named in config['web'][key]."""
    rel = (cfg.get("web") or {}).get(key)
    if not rel:
        return None
    p = Path(os.path.expanduser(rel))
    return p if p.is_absolute() else REPO_ROOT / p
