#!/usr/bin/env python3
"""
thrash_server.py — local control backend for the Thrash panel + menu bar.

One small stdlib HTTP server that unifies every local model lane behind a
single JSON API, so the HTML panel and the Swift menu bar app both talk to the
same source of truth.

All lane control and status logic lives in core.py (shared with the CLI, the
doctor, the TTL reaper and the tests); this module is only the HTTP surface:
routing, JSON encoding and the uzu SSE proxy.

API:
  GET  /api/status        -> { memory, lanes:[{id,label,kind,on,port,ram_mb,impact}] }
  GET  /api/health        -> { ok: true }
  GET  /  /panel          -> the control panel HTML
  GET  /uzu-chat          -> the streaming chat HTML
  POST /api/toggle        body {"id":"...","on":true|false} -> {ok, message, status}
  POST /api/uzu-chat      -> SSE stream-proxy to the configured uzu lane

Stdlib only. Run under launchd (see docs/launchd.md) or directly:
    python3 server/thrash_server.py
"""
import json
import os
import sys
import types
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as config_mod
import core
import notify
import reaper

# Re-exported so existing callers (and tests/test_lanes.py) can keep reaching
# the shared logic through this module. core owns the state; these names are
# the same objects, not copies.
from core import (  # noqa: F401
    CFG,
    HOST,
    PORT,
    RAMTOGGLE,
    SCRIPT_LANES,
    UZU_PROXY_LANE,
    api_config,
    get_status,
    lane_log_path,
    ramtoggle_status,
    read_log_tail,
    run,
    script_lane_on,
    toggle,
    touch_activity,
    uzu_proxy_port,
)

# core.toggle() / core.ramtoggle_status() read these globals out of core at
# call time, so a caller that patches them by name on this module (which is
# how the lane-control tests drive the lane state) has to reach core, or the
# patch silently does nothing. Assignment is forwarded ONE WAY (this module
# -> core); core's state is what the shared logic reads, so patching core
# directly is the canonical path for new code (CLI/doctor/reaper).
_CORE_STATE = ("CFG", "PORT", "HOST", "RAMTOGGLE", "SCRIPT_LANES", "UZU_PROXY_LANE")


class _ServerModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _CORE_STATE:
            setattr(core, name, value)


sys.modules[__name__].__class__ = _ServerModule


def _load_asset(key, fallback_title):
    path = config_mod.web_path(core.CFG, key)
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<html><body><h1>{fallback_title}</h1><p>asset missing</p></body></html>"


PANEL_HTML = _load_asset("panel", "Thrash")
UZU_CHAT_HTML = _load_asset("uzu_chat", "uzu chat")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _read_body(self, max_bytes=10 * 1024 * 1024):
        """Read and return the request body, or None after sending an error."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json(400, {"error": "bad content-length"})
            return None
        if n < 0:
            self._json(400, {"error": "bad content-length"})
            return None
        if n > max_bytes:
            self._json(413, {"error": "request too large"})
            return None
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        # rstrip the path component only — the query string can legitimately
        # end with '/' (e.g. ?id=lane1/), and self.path is "path?query".
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/status":
            self._json(200, core.get_status())
        elif path == "/api/health":
            self._json(200, {"ok": True})
        elif path == "/api/logs":
            self._serve_logs(parsed)
        elif path == "/api/config":
            self._json(200, core.api_config())
        elif path in ("", "/index.html", "/panel"):
            self._html(200, PANEL_HTML)
        elif path in ("/uzu-chat", "/uzu-chat.html"):
            self._html(200, UZU_CHAT_HTML)
        else:
            self._json(404, {"error": "not found"})

    def _serve_logs(self, parsed=None):
        """GET /api/logs?id=<lane>&lines=N -> the lane's log tail.

        The path resolution and tail read live in core.read_log_tail() so the
        CLI can reuse them; this only shapes the HTTP contract.
        """
        if parsed is None:
            parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        lane_id = (params.get("id") or [""])[0].strip()
        if not lane_id:
            self._json(400, {"error": "missing required query param: id"})
            return
        lane = core.SCRIPT_LANES.get(lane_id)
        if not lane:
            self._json(404, {"error": f"unknown lane: {lane_id}"})
            return
        raw_lines = (params.get("lines") or [None])[0]
        path, exists, tail = core.read_log_tail(lane_id, raw_lines)
        resp = {
            "id": lane_id,
            "path": path,
            "exists": exists,
            "tail": tail,
            "script": lane.get("script"),
        }
        if not exists:
            # Lanes with no configured `log` write wherever their script
            # chooses (by convention $HOME/.hermes/logs/), which we cannot
            # map reliably — surface the script so the UI can say where.
            resp["note"] = (
                "no log file: this lane declares no log path in config"
                if path is None else
                f"log file not found (or unreadable): {path}"
            )
        self._json(200, resp)

    def do_POST(self):
        if self.path == "/api/toggle":
            body = self._read_body()
            if body is None:
                return
            try:
                req = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            ok, msg = core.toggle(req.get("id"), bool(req.get("on")))
            if ok and bool(req.get("on")):
                # Activity signal for the idle reaper: a toggle-on counts as
                # Thrash-mediated use of the lane from this moment on.
                core.touch_activity(req.get("id"))
                # Background watch: notify when the lane finishes loading.
                # notify.watch_lane_load() spawns a daemon thread and is a
                # no-op when on_load is disabled or the lane is unknown —
                # it never blocks or raises into the request.
                notify.watch_lane_load(req.get("id"))
            self._json(200, {"ok": ok, "message": msg, "status": core.get_status()})
        elif self.path == "/api/uzu-chat":
            self._proxy_uzu_chat()
        else:
            self._json(404, {"error": "not found"})

    def _proxy_uzu_chat(self):
        """Stream-proxy to a chosen lane's OpenAI endpoint (SSE + CORS).

        The POST body may name a ``lane`` (a configured script-lane id) or a
        ``port``; when present and valid the proxy targets that lane instead
        of the configured ``uzu_proxy_lane``. This is what lets the chat UI
        talk to any loaded lane, not just the uzu default. The ``model``
        field is passed through untouched.
        """
        body = self._read_body()
        if body is None:
            return
        if not body:
            body = b"{}"
        lane_id = None
        try:
            lane_id = json.loads(body).get("lane")
        except (json.JSONDecodeError, AttributeError):
            lane_id = None
        port = core.chat_lane_port(lane_id)
        if port is None:
            # lane_id was given but unknown/invalid -> 400; absent -> default
            if lane_id:
                self._json(400, {"error": f"unknown or non-script lane: {lane_id}"})
            else:
                self._json(502, {"error": "no uzu_proxy_lane configured"})
            return
        upstream = None
        headers_sent = False
        # Activity signal for the idle reaper: a proxied chat message counts
        # as use of the target lane, so it does not get reaped mid-conversation.
        core.touch_activity(lane_id or core.UZU_PROXY_LANE)
        try:
            rreq = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream"})
            upstream = urllib.request.urlopen(rreq, timeout=300)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # SSE is close-delimited (no Content-Length): the stream ends when
            # this handler returns and the socket closes, so advertise close
            # rather than keep-alive — otherwise a client waits for more.
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            headers_sent = True
            self.close_connection = True
            while True:
                chunk = upstream.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:  # noqa: BLE001
            # Once headers are committed we cannot send a real error response;
            # just stop. Otherwise report the lane as unreachable.
            if not headers_sent:
                self._json(502, {"error": f"uzu lane unreachable: {e}"})
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    # Idle auto-unload (TTL reaper): started ONLY when running as the server,
    # never on import — tests import this module and must not spawn threads.
    # core.stop_idle_lanes() no-ops unless idle_unload.enabled is true.
    _reaper = None
    if core.CFG["idle_unload"]["enabled"]:
        _reaper = reaper.Reaper()
        _reaper.start()
        cfg = core.idle_unload_cfg()
        print(f"reaper: on — unload after {cfg.get('ttl_minutes')}min idle "
              f"(lanes={cfg.get('lanes')})", flush=True)
    # Low-memory notifications: started ONLY when running as the server,
    # never on import (same rule as the reaper — tests import this module
    # and must not spawn threads).
    _notifier = None
    if core.CFG["notifications"]["on_low_memory"]:
        _notifier = notify.start_low_memory_watch()
        print(f"notify: on — alert below "
              f"{core.CFG['notifications'].get('low_memory_gb')}GB free",
              flush=True)
    try:
        srv = ThreadingHTTPServer((core.HOST, core.PORT), Handler)
        print(f"thrash server on {core.HOST}:{core.PORT}", flush=True)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _reaper is not None:
            _reaper.stop()
        if _notifier is not None:
            _notifier.stop()
