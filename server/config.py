"""
config.py — ModelDeck configuration loader.

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
    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def web_path(cfg, key):
    """Absolute path to a web asset named in config['web'][key]."""
    rel = cfg.get("web", {}).get(key)
    if not rel:
        return None
    p = Path(os.path.expanduser(rel))
    return p if p.is_absolute() else REPO_ROOT / p
