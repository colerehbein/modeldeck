"""Tests for the Thrash HTTP surface: GET /api/logs and GET /api/config.

Both endpoints run against a real ThreadingHTTPServer bound to an ephemeral
port ('127.0.0.1', 0) rather than by calling the Handler directly, so the
routing, query parsing and status codes are exercised end to end over the
socket. Like test_lanes.py, the module is imported once under a throwaway
config so the machine's real config.local.json is never read.

The log-tail and config payload logic lives in core (reused by the CLI), so
the HTTP tests deliberately drive core.SCRIPT_LANES through the server
module — the one-way forwarding makes those assignments reach core, which is
exactly what read_log_tail()/api_config() read at request time.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from tests import helpers
except ImportError:  # running with tests/ as top-level dir
    import helpers

_import_tmp = tempfile.TemporaryDirectory()
helpers.write_config(_import_tmp.name, [], ramtoggle=None)
import core  # noqa: E402
import thrash_server  # noqa: E402

helpers.restore_config()


def tearDownModule():
    helpers.restore_config()
    _import_tmp.cleanup()


class ServerEndpointTestCase(unittest.TestCase):
    """Shared harness: an ephemeral-port server plus a stub lane."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self._orig_lanes = core.SCRIPT_LANES
        self._orig_port = core.PORT
        self._orig_host = core.HOST
        self._orig_uzu = core.UZU_PROXY_LANE

        self.srv = ThreadingHTTPServer(
            ("127.0.0.1", 0), thrash_server.Handler)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self.srv.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        core.SCRIPT_LANES = self._orig_lanes
        core.PORT = self._orig_port
        core.HOST = self._orig_host
        core.UZU_PROXY_LANE = self._orig_uzu
        self.srv.shutdown()
        self.srv.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    # ---- request helpers -------------------------------------------------

    def get(self, path):
        """GET a path; returns (status, parsed_json_body)."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    # ---- lane fixtures ---------------------------------------------------

    def log_path(self, name="lane.log"):
        return os.path.join(self.tmpdir, name)

    def write_log(self, body, name="lane.log"):
        path = self.log_path(name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def stub_lane(self, log=None, lane_id="lane1", port=8801, kind="script"):
        """Install a single stub lane in core.SCRIPT_LANES, return its dict."""
        lane = {"id": lane_id, "label": lane_id, "kind": kind, "port": port,
                "approx_mb": 1000, "impact": "",
                "script": os.path.join(self.tmpdir, lane_id + ".sh")}
        if log is not None:
            lane["log"] = log
        core.SCRIPT_LANES = {lane_id: lane}
        return lane


class LogsEndpointTest(ServerEndpointTestCase):
    def test_returns_tail_of_known_log(self):
        path = self.write_log("a\nb\nc\nd\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1")
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "lane1")
        self.assertEqual(body["path"], path)
        self.assertTrue(body["exists"])
        self.assertEqual(body["tail"], "a\nb\nc\nd")

    def test_respects_lines_param(self):
        path = self.write_log("a\nb\nc\nd\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["tail"].split("\n"), ["c", "d"])

    def test_lines_clamped_to_max(self):
        body_text = "\n".join(f"line{i}" for i in range(10)) + "\n"
        path = self.write_log(body_text)
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=99999")
        self.assertEqual(status, 200)
        # clamped to core._LOG_MAX_LINES (5000), so a 10-line file returns all
        self.assertEqual(len(body["tail"].split("\n")), 10)
        self.assertTrue(body["tail"].endswith("line9"))

    def test_lines_clamped_to_min(self):
        path = self.write_log("a\nb\nc\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["tail"], "c")

    def test_non_numeric_lines_falls_back_to_default(self):
        path = self.write_log("a\nb\nc\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=abc")
        self.assertEqual(status, 200)
        self.assertEqual(body["tail"], "a\nb\nc")

    def test_missing_lines_uses_default(self):
        path = self.write_log("a\nb\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1")
        self.assertEqual(status, 200)
        self.assertEqual(body["tail"], "a\nb")

    def test_lane_without_log_path_reports_not_exists(self):
        self.stub_lane(log=None)
        status, body = self.get("/api/logs?id=lane1")
        self.assertEqual(status, 200)
        self.assertIsNone(body["path"])
        self.assertFalse(body["exists"])
        self.assertEqual(body["tail"], "")
        # the script path is surfaced so the UI can hint where logs go
        self.assertTrue(body["script"].endswith("lane1.sh"))
        self.assertIn("note", body)

    def test_declared_but_absent_log_file(self):
        self.stub_lane(log=self.log_path("never-written.log"))
        status, body = self.get("/api/logs?id=lane1")
        self.assertEqual(status, 200)
        self.assertEqual(body["path"], self.log_path("never-written.log"))
        self.assertFalse(body["exists"])
        self.assertEqual(body["tail"], "")

    def test_unknown_lane_is_404(self):
        self.stub_lane()
        status, body = self.get("/api/logs?id=nope")
        self.assertEqual(status, 404)
        self.assertIn("unknown lane", body["error"])

    def test_missing_id_is_400(self):
        self.stub_lane()
        status, body = self.get("/api/logs")
        self.assertEqual(status, 400)
        self.assertIn("id", body["error"])

    def test_empty_id_is_400(self):
        self.stub_lane()
        status, body = self.get("/api/logs?id=")
        self.assertEqual(status, 400)
        self.assertIn("id", body["error"])


class LogsEndpointLargeFileTest(ServerEndpointTestCase):
    """The >5MB seek path in core.read_log_tail, exercised over HTTP."""

    def test_tail_of_large_log(self):
        path = self.log_path("big.log")
        total = 60_000  # > 5MB with this line size
        line = "x" * 100
        with open(path, "w", encoding="utf-8") as f:
            for i in range(total):
                f.write(f"{i:05d}{line}\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=3")
        self.assertEqual(status, 200)
        self.assertTrue(body["exists"])
        self.assertEqual(
            body["tail"].split("\n")[-1], f"{total - 1:05d}{line}")

    def test_tail_of_large_log_single_very_long_line(self):
        """One 6MB line: the block must grow until it holds the whole thing."""
        path = self.log_path("one-big-line.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("y" * (6 * 1024 * 1024))
            f.write("\n")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=5")
        self.assertEqual(status, 200)
        self.assertEqual(body["tail"], "y" * (6 * 1024 * 1024))

    def test_tail_of_large_log_with_trailing_partial_line(self):
        """Last line has no newline: it still counts as a line."""
        path = self.log_path("partial.log")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(70_000):
                f.write(f"{i:06d}" + "z" * 94 + "\n")
            f.write("tail-without-newline")
        self.stub_lane(log=path)
        status, body = self.get("/api/logs?id=lane1&lines=2")
        self.assertEqual(status, 200)
        lines = body["tail"].split("\n")
        self.assertEqual(lines[-1], "tail-without-newline")


class ConfigEndpointTest(ServerEndpointTestCase):
    def test_reflects_core_host_and_port(self):
        core.HOST = "127.0.0.1"
        core.PORT = 12345
        core.SCRIPT_LANES = {}
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["api_base"], "http://127.0.0.1:12345")
        self.assertEqual(body["panel_port"], 12345)
        self.assertEqual(body["lanes"], [])

    def test_lists_stub_lanes(self):
        core.SCRIPT_LANES = {
            "a": {"id": "a", "label": "A", "kind": "script", "port": 8801,
                  "approx_mb": 1000, "script": "x"},
            "b": {"id": "b", "label": "B", "kind": "ramtoggle", "port": 8802,
                  "approx_mb": None, "script": "y"},
        }
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual([l["id"] for l in body["lanes"]], ["a", "b"])
        self.assertEqual(body["lanes"][0], {
            "id": "a", "label": "A", "port": 8801,
            "kind": "script", "approx_mb": 1000, "model": None})
        self.assertEqual(body["lanes"][1], {
            "id": "b", "label": "B", "port": 8802,
            "kind": "ramtoggle", "approx_mb": None, "model": None})

    def test_uzu_port_from_configured_proxy_lane(self):
        core.UZU_PROXY_LANE = "uzu"
        core.SCRIPT_LANES = {"uzu": {"id": "uzu", "label": "Uzu",
                                     "port": 8096, "script": "x"}}
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["uzu_port"], 8096)

    def test_uzu_port_none_without_proxy_lane(self):
        core.UZU_PROXY_LANE = None
        core.SCRIPT_LANES = {}
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertIsNone(body["uzu_port"])

    def test_panel_port_matches_the_served_instance(self):
        """panel_port is core.PORT even when the test server is elsewhere."""
        core.PORT = 4321
        core.SCRIPT_LANES = {}
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["panel_port"], 4321)


class _Upstream:
    """A tiny raw-socket server standing in for a lane's OpenAI endpoint.

    Accepts one connection per request, reads the headers, replies with a
    short SSE body, then closes the socket — so the proxy's read-until-EOF
    loop terminates promptly. Records each hit so a test can assert which
    lane the chat proxy forwarded to.
    """

    def __init__(self):
        import socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(5)
        self.port = self._sock.getsockname()[1]
        self.hits = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            try:
                conn.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                # read any body advertised by Content-Length
                head, _, rest = data.partition(b"\r\n\r\n")
                clen = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        clen = int(line.split(b":", 1)[1].strip())
                while len(rest) < clen:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                self.hits.append(head.split(b"\r\n")[0])
                body = (b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Connection: close\r\n\r\n"
                        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                        b"data: [DONE]\n\n")
                conn.sendall(body)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


class ChatProxyRoutingTest(ServerEndpointTestCase):
    """The chat proxy must forward to the lane named in the POST body."""

    def setUp(self):
        super().setUp()
        self._orig_uzu = core.UZU_PROXY_LANE
        self.up_a = _Upstream()
        self.up_b = _Upstream()
        core.SCRIPT_LANES = {
            "laneA": {"id": "laneA", "label": "A", "kind": "script",
                      "port": self.up_a.port, "approx_mb": 100,
                      "impact": "", "script": "x"},
            "laneB": {"id": "laneB", "label": "B", "kind": "script",
                      "port": self.up_b.port, "approx_mb": 100,
                      "impact": "", "script": "x"},
        }
        core.UZU_PROXY_LANE = "laneA"  # default target when no lane given

    def tearDown(self):
        core.UZU_PROXY_LANE = self._orig_uzu
        self.up_a.close()
        self.up_b.close()
        super().tearDown()

    def _post_chat(self, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + "/api/uzu-chat", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_forwards_to_named_lane(self):
        status, _ = self._post_chat(
            {"lane": "laneB", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.up_b.hits), 1, "laneB should have been hit")
        self.assertEqual(len(self.up_a.hits), 0, "default laneA must NOT be hit")

    def test_defaults_to_proxy_lane_without_lane_field(self):
        status, _ = self._post_chat(
            {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.up_a.hits), 1, "proxy lane should be the default")

    def test_unknown_lane_is_400(self):
        status, body = self._post_chat(
            {"lane": "does-not-exist", "messages": []})
        self.assertEqual(status, 400)
        self.assertIn("unknown", json.loads(body.decode())["error"])


if __name__ == "__main__":
    unittest.main()
