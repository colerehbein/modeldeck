#!/usr/bin/env python3
"""
modeldeck_server.py — local control backend for the ModelDeck panel + menu bar.

One small stdlib HTTP server that unifies every local model lane behind a
single JSON API, so the HTML panel and the Swift menu bar app both talk to the
same source of truth.

It merges two lane sources:
  1. ramtoggle --json   (launchd / LM Studio / router / ComfyUI lanes), if
     a ramtoggle path is configured
  2. the lane scripts declared in config/*.json (llama.cpp + uzu models)

API:
  GET  /api/status        -> { memory, lanes:[{id,label,kind,on,port,ram_mb,impact}] }
  GET  /api/health        -> { ok: true }
  GET  /  /panel          -> the control panel HTML
  GET  /uzu-chat          -> the streaming chat HTML
  POST /api/toggle        body {"id":"...","on":true|false} -> {ok, message, status}
  POST /api/uzu-chat      -> SSE stream-proxy to the configured uzu lane

Lane control:
  - script lanes  -> `<script> start|stop`, status via `<script> status`
  - ramtoggle lanes -> `ramtoggle on|off <id>`, status via `ramtoggle --json`

Stdlib only. Run under launchd (see docs/launchd.md) or directly:
    python3 server/modeldeck_server.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as config_mod

CFG = config_mod.load()
PORT = int(CFG["server"]["port"])
HOST = CFG["server"]["host"]
RAMTOGGLE = CFG.get("ramtoggle") or None
SCRIPT_LANES = {l["id"]: l for l in CFG.get("lanes", [])}
UZU_PROXY_LANE = CFG.get("uzu_proxy_lane")


def _load_asset(key, fallback_title):
    path = config_mod.web_path(CFG, key)
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<html><body><h1>{fallback_title}</h1><p>asset missing</p></body></html>"


PANEL_HTML = _load_asset("panel", "ModelDeck")
UZU_CHAT_HTML = _load_asset("uzu_chat", "uzu chat")


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def script_lane_on(lane):
    """Ask the lane script for ground truth (on-demand servers may be down)."""
    rc, out, _ = run([lane["script"], "status"], timeout=15)
    return any(line.strip().startswith("UP") for line in out.splitlines())


def ramtoggle_status():
    """Return (lanes, memory) from ramtoggle --json, or ([], {}) if unavailable."""
    if not RAMTOGGLE:
        return [], {}
    rc, out, _ = run([RAMTOGGLE, "--json"], timeout=30)
    if rc != 0:
        return [], {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], {}
    lanes = [{
        "id": l["id"],
        "label": l["label"],
        "kind": l.get("kind", "ramtoggle"),
        "on": bool(l.get("on")),
        "port": l.get("port"),
        "ram_mb": l.get("approx_mb"),
        "impact": l.get("impact", ""),
    } for l in data.get("lanes", [])]
    return lanes, data.get("memory", {})


def get_status():
    rt_lanes, memory = ramtoggle_status()
    lanes = list(rt_lanes)
    for lane in CFG.get("lanes", []):
        lanes.append({
            "id": lane["id"],
            "label": lane["label"],
            "kind": lane.get("kind", "script"),
            "on": script_lane_on(lane),
            "port": lane.get("port"),
            "ram_mb": lane.get("approx_mb"),
            "impact": lane.get("impact", ""),
        })
    return {"memory": memory, "lanes": lanes, "ts": int(time.time())}


def toggle(lane_id, turn_on):
    if lane_id in SCRIPT_LANES:
        script = SCRIPT_LANES[lane_id]["script"]
        if turn_on:
            # start blocks while the model loads; spawn detached, then verify
            # the process actually launched (a failed exec is otherwise silent).
            proc = subprocess.Popen(
                [script, "start"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.5)
            if proc.poll() is not None:
                return False, (f"{lane_id} failed to start (exit {proc.returncode}) "
                               "— see the lane log")
            return True, f"{lane_id} starting (loads in background)"
        rc, out, _ = run([script, "stop"], timeout=20)
        return rc == 0, out.strip()
    if RAMTOGGLE:
        rt_lanes, _ = ramtoggle_status()
        if lane_id not in {l["id"] for l in rt_lanes}:
            return False, f"unknown lane: {lane_id}"
        action = "on" if turn_on else "off"
        rc, out, _ = run([RAMTOGGLE, action, lane_id], timeout=60)
        return rc == 0, out.strip()
    return False, f"unknown lane: {lane_id} (no ramtoggle configured)"


def uzu_proxy_port():
    if not UZU_PROXY_LANE:
        return None
    lane = SCRIPT_LANES.get(UZU_PROXY_LANE)
    return lane.get("port") if lane else None


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
        if n < 0 or n > max_bytes:
            self._json(413, {"error": "request too large"})
            return None
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/status":
            self._json(200, get_status())
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
            ok, msg = toggle(req.get("id"), bool(req.get("on")))
            self._json(200, {"ok": ok, "message": msg, "status": get_status()})
        elif self.path == "/api/uzu-chat":
            self._proxy_uzu_chat()
        else:
            self._json(404, {"error": "not found"})

    def _proxy_uzu_chat(self):
        """Stream-proxy to the configured uzu lane (SSE passthrough + CORS)."""
        port = uzu_proxy_port()
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
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"modeldeck server on {HOST}:{PORT}", flush=True)
    srv.serve_forever()
