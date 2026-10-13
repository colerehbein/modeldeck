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
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
EXAMPLE = CONFIG_DIR / "config.example.json"
LOCAL = CONFIG_DIR / "config.local.json"

# Keys whose string values are filesystem paths (safe to expanduser).
_PATH_KEYS = {"ramtoggle", "script"}


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
    """Script paths in lane defs are repo-relative; make them absolute."""
    for lane in cfg.get("lanes", []):
        script = lane.get("script")
        if script and not os.path.isabs(os.path.expanduser(script)):
            lane["script"] = str(REPO_ROOT / script)
    return cfg


def _validate(cfg):
    """Fail loudly at startup with a clear message on missing required keys."""
    missing = []
    if "host" not in cfg.get("server", {}):
        missing.append("server.host")
    if "port" not in cfg.get("server", {}):
        missing.append("server.port")
    for i, lane in enumerate(cfg.get("lanes", [])):
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
_FEATURE_DEFAULTS = {
    "idle_unload": {
        "enabled": False,
        "ttl_minutes": 30,
        "lanes": ["*"],  # "*" = every lane; otherwise a list of lane ids
    },
    "notifications": {
        "on_load": True,
        "on_low_memory": True,
        "low_memory_gb": 6,
    },
    "ram_guard": {
        "mode": "warn",  # "warn" | "block"
    },
}

_RAM_GUARD_MODES = ("warn", "block")


def _is_number(value):
    """True for a real JSON number (bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _apply_feature_defaults(cfg):
    """Fill missing optional keys with defaults, then validate supplied values.

    Fills first so a partially-specified block is never rejected for a key the
    user did not set; only explicitly-provided values are checked.
    """
    for key, defaults in _FEATURE_DEFAULTS.items():
        section = cfg.setdefault(key, {})
        if not isinstance(section, dict):
            raise SystemExit(
                f"config error — {key} must be an object, got "
                f"{type(section).__name__}"
            )
        for sub, default in defaults.items():
            section.setdefault(sub, default)

    idle = cfg["idle_unload"]
    if not isinstance(idle["ttl_minutes"], int) or isinstance(
        idle["ttl_minutes"], bool
    ) or idle["ttl_minutes"] <= 0:
        raise SystemExit(
            "config error — idle_unload.ttl_minutes must be an integer > 0, "
            f"got {idle['ttl_minutes']!r}"
        )
    if not isinstance(idle["lanes"], list):
        raise SystemExit(
            "config error — idle_unload.lanes must be a list of lane ids "
            f'(or ["*"]), got {idle["lanes"]!r}'
        )

    notes = cfg["notifications"]
    if not _is_number(notes["low_memory_gb"]) or notes["low_memory_gb"] <= 0:
        raise SystemExit(
            "config error — notifications.low_memory_gb must be a number > 0, "
            f"got {notes['low_memory_gb']!r}"
        )

    if cfg["ram_guard"]["mode"] not in _RAM_GUARD_MODES:
        raise SystemExit(
            "config error — ram_guard.mode must be one of "
            + " | ".join(_RAM_GUARD_MODES)
            + f", got {cfg['ram_guard']['mode']!r}"
        )
    return cfg


def feature_cfg(cfg, key):
    """Merged view of an optional feature block, defaulted.

    load() already populates every block, so this just returns the live dict;
    it exists for callers handed a config that predates these keys.
    """
    defaults = _FEATURE_DEFAULTS.get(key)
    if defaults is None:
        raise KeyError(f"unknown feature block: {key!r}")
    section = cfg.get(key)
    if not isinstance(section, dict):
        return dict(defaults)
    merged = dict(defaults)
    merged.update(section)
    return merged


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
    for lane in cfg.get("lanes", []):
        lane.setdefault("label", lane["id"])
        lane.setdefault("impact", "")
        lane.setdefault("approx_mb", None)
    # Optional per-lane log path (string); no validation beyond type.
    for lane in cfg.get("lanes", []):
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
    rel = cfg.get("web", {}).get(key)
    if not rel:
        return None
    p = Path(os.path.expanduser(rel))
    return p if p.is_absolute() else REPO_ROOT / p
