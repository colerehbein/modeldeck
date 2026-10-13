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
import subprocess
import sys
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as config_mod
import core

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
    get_status,
    ramtoggle_status,
    run,
    script_lane_on,
    toggle,
    uzu_proxy_port,
)

# core.toggle() / core.ramtoggle_status() read these globals out of core at
# call time, so a caller that patches them by name on this module (which is
# how the lane-control tests drive the lane state) has to reach core, or the
# patch silently does nothing. Forward the assignment in both directions:
# the local attribute stays readable, core's state actually changes.
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
        path = self.path.rstrip("/")
        if path == "/api/status":
            self._json(200, core.get_status())
        elif path == "/api/health":
            self._json(200, {"ok": True})
        elif path in ("", "/index.html", "/panel"):
            self._html(200, PANEL_HTML)
        elif path in ("/uzu-chat", "/uzu-chat.html"):
            self._html(200, UZU_CHAT_HTML)
        else:
            self._json(404, {"error": "not found"})

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
            self._json(200, {"ok": ok, "message": msg, "status": core.get_status()})
        elif self.path == "/api/uzu-chat":
            self._proxy_uzu_chat()
        else:
            self._json(404, {"error": "not found"})

    def _proxy_uzu_chat(self):
        """Stream-proxy to the configured uzu lane (SSE passthrough + CORS)."""
        port = core.uzu_proxy_port()
        if not port:
            self._json(502, {"error": "no uzu_proxy_lane configured"})
            return
        body = self._read_body()
        if body is None:
            return
        if not body:
            body = b"{}"
        upstream = None
        headers_sent = False
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
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            headers_sent = True
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
    srv = ThreadingHTTPServer((core.HOST, core.PORT), Handler)
    print(f"thrash server on {core.HOST}:{core.PORT}", flush=True)
    srv.serve_forever()
