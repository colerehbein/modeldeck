"""
config.py — ModelDeck configuration loader.

Loads config/config.example.json as the base, then overlays
config/config.local.json (gitignored) if present. Expands `~` in paths and
resolves script/web paths relative to the repo root.

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


def _expand(value):
    """Recursively expand `~` and resolve repo-relative paths in strings."""
    if isinstance(value, str):
        v = os.path.expanduser(value)
        return v
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _resolve_script_paths(cfg):
    """Script paths in lane defs are repo-relative; make them absolute."""
    for lane in cfg.get("lanes", []):
        script = lane.get("script")
        if script and not os.path.isabs(os.path.expanduser(script)):
            lane["script"] = str(REPO_ROOT / script)
    return cfg


def load():
    with open(EXAMPLE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if LOCAL.exists():
        with open(LOCAL, "r", encoding="utf-8") as f:
            local = json.load(f)
        cfg.update(local)
    cfg = _expand(cfg)
    cfg = _resolve_script_paths(cfg)
    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def web_path(cfg, key):
    """Absolute path to a web asset named in config['web'][key]."""
    rel = cfg.get("web", {}).get(key)
    if not rel:
        return None
    p = Path(os.path.expanduser(rel))
    return p if p.is_absolute() else REPO_ROOT / p
