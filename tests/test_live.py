"""Živé video: RTSP zůstává na serveru, prohlížeč dostane jen WebSocket jedné kamery."""
from http.client import parse_headers
from io import BytesIO
from pathlib import Path
import base64
import hashlib
import os
import socket
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "iot"))
from access import remote_route_allowed
from iot import IoTManager
from iot_driver import IoTError
from live import ensure_stream, go2rtc_endpoint, live_upgrade_header, proxy_websocket, rtsp_source, websocket_accept
from server import make_server


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "p@ss/word"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeCamera:
    def status(self, module):
        return {"online": True, "model": "C320WS", "ip": module["ip"]}

    def snapshot(self, module):
        return b"\xff\xd8test\xff\xd9"


class SourceTests(unittest.TestCase):
    def test_rtsp_source_quotes_secrets_and_stays_on_substream(self):
        module = {"ip": "192.168.1.45", "tapo_username": "cam@era", "tapo_password": PASSWORD}
        source = rtsp_source(module)
        self.assertEqual(source, "rtsp://cam%40era:p%40ss%2Fword@192.168.1.45:554/stream2#transport=tcp")
        self.assertNotIn(PASSWORD, source)

    def test_public_ip_is_rejected(self):
        with self.assertRaises(IoTError):
            rtsp_source({"ip": "8.8.8.8", "tapo_username": "user", "tapo_password": "secret"})

    def test_go2rtc_must_be_loopback(self):
        host, port = go2rtc_endpoint("http://127.0.0.1:1984")
        self.assertEqual((host, port), ("127.0.0.1", 1984))
        for raw in ("http://192.168.1.3:1984", "https://127.0.0.1:1984", "http://user:pass@127.0.0.1:1984"):
            with self.assertRaises(IoTError):
                go2rtc_endpoint(raw)

    def test_upgrade_detection_ignores_foreign_targets(self):
        good = (
            "GET /api/iot/devices/238040bf6c4e/live?src=exec:bad HTTP/1.1\r\n"
            "Host: iot.hanzhub.cz\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        ).encode()
        evil = b"GET /api/ws?src=exec:bad HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        plain = b"GET /api/iot/devices/238040bf6c4e/live HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        self.assertTrue(live_upgrade_header(good))
        self.assertFalse(live_upgrade_header(evil))
        self.assertFalse(live_upgrade_header(plain))

    def test_remote_allowlist_includes_live_only_as_get(self):
        path = "/api/iot/devices/238040bf6c4e/live"
        self.assertTrue(remote_route_allowed("GET", path))
        self.assertFalse(remote_route_allowed("POST", path))
        self.assertFalse(remote_route_allowed("GET", "/api/streams"))
        self.assertFalse(remote_route_allowed("GET", "/api/ws"))

    def test_compose_and_go2rtc_config_have_no_camera_url(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        config = (ROOT / "iot/go2rtc.yaml").read_text(encoding="utf-8")
        self.assertIn("alexxit/go2rtc:1.9.15", compose)
        self.assertIn("network_mode: host", compose)
        self.assertIn('listen: "127.0.0.1:1984"', config)
        self.assertIn("/api/ws", config)
        self.assertNotIn("/api/config", config)
        for text in (compose, config):
            self.assertNotIn("rtsp://", text)
            self.assertNotIn(PASSWORD, text)


class _Response:
    def __init__(self, status=204):
        self.status = status

    def read(self, _n=None):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class EnsureStreamTests(unittest.TestCase):
    def test_patch_goes_to_loopback_and_does_not_log_the_url(self):
        seen = []

        def opener(request, timeout=None):
            seen.append((request.get_method(), request.full_url, timeout))
            return _Response()

        module = {"id": "238040bf6c4e", "ip": "192.168.1.45",
                  "tapo_username": "cam@era", "tapo_password": PASSWORD}
        stderr = sys.stderr
        buffer = BytesIO()
        sys.stderr = buffer
        try:
            ensure_stream(module, opener=opener)
        finally:
            sys.stderr = stderr
        self.assertEqual(buffer.getvalue(), b"")
        self.assertEqual(len(seen), 1)
        method, url, timeout = seen[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(timeout, 5)
        parsed = urlsplit(url)
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parsed.path, "/api/streams")
        query = parse_qs(parsed.query)
        self.assertEqual(query["name"], ["238040bf6c4e"])
        self.assertEqual(query["src"], ["rtsp://cam%40era:p%40ss%2Fword@192.168.1.45:554/stream2#transport=tcp"])
        self.assertNotIn(PASSWORD, url)

    def test_failure_body_is_not_returned(self):
        def opener(request, timeout=None):
            raise HTTPError(request.full_url, 400, "bad", hdrs=None, fp=BytesIO(b"rtsp://cam%40era:secret@192.168.1.45"))

        module = {"id": "238040bf6c4e", "ip": "192.168.1.45",
                  "tapo_username": "cam", "tapo_password": "secret"}
        with self.assertRaises(IoTError) as caught:
            ensure_stream(module, opener=opener)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("rtsp", str(caught.exception))
        self.assertEqual(caught.exception.status, 502)


class LiveProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = IoTManager(data_dir=self.temp.name, devices_file=Path(self.temp.name) / "missing.json",
                                  camera_adapter=FakeCamera())
        self.module = self.manager.add_module({
            "name": "Zahrada", "driver": "tapo_c320ws", "ip": "192.168.1.45",
            "tapo_username": "cam@era", "tapo_password": PASSWORD,
        })
        self.seen = []
        self.fake = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.fake.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.fake.bind(("127.0.0.1", 0))
        self.fake.listen(8)
        self.fake_port = self.fake.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve_go2rtc, daemon=True)
        self.thread.start()
        self.server = make_server("127.0.0.1", 0, self.manager)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        os.environ["HANZHUB_GO2RTC_URL"] = f"http://127.0.0.1:{self.fake_port}"

    def tearDown(self):
        self.stop.set()
        try:
            self.fake.close()
        except OSError:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        os.environ.pop("HANZHUB_GO2RTC_URL", None)
        self.temp.cleanup()

    def _serve_go2rtc(self):
        while not self.stop.is_set():
            try:
                self.fake.settimeout(0.2)
                conn, _ = self.fake.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_upstream, args=(conn,), daemon=True).start()

    def _handle_upstream(self, conn):
        try:
            data = b""
            conn.settimeout(2)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.seen.append(data)
            if data.startswith(b"PATCH ") or data.startswith(b"DELETE "):
                conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if not data.startswith(b"GET /api/ws?src="):
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            key = parse_headers(BytesIO(data.split(b"\r\n", 1)[1])).get("Sec-WebSocket-Key")
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            conn.settimeout(2)
            echoed = conn.recv(4096)
            if echoed:
                conn.sendall(echoed)
        except OSError:
            pass
        finally:
            conn.close()

    def request(self, path):
        try:
            response = urlopen(f"http://127.0.0.1:{self.server.server_port}{path}", timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_plain_get_does_not_open_go2rtc_or_leak_the_password(self):
        code, body = self.request(f"/api/iot/devices/{self.module['id']}/live")
        self.assertEqual(code, 426)
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertNotIn(b"rtsp://", body)
        self.assertEqual(self.seen, [])
        self.manager.update_module(self.module["id"], {"enabled": False})
        code, body = self.request(f"/api/iot/devices/{self.module['id']}/live")
        self.assertEqual(code, 409)
        self.assertNotIn(PASSWORD.encode(), body)

    def test_websocket_proxies_only_named_stream(self):
        key = base64.b64encode(os.urandom(16)).decode()
        path = f"/api/iot/devices/{self.module['id']}/live?src=exec:bad"
        host = f"127.0.0.1:{self.server.server_port}"
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: Upgrade\r\n"
            f"Upgrade: websocket\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            client.sendall(request)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                self.assertTrue(chunk)
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            self.assertIn(b" 101 ", head.split(b"\r\n", 1)[0])
            self.assertIn(b"Sec-WebSocket-Accept: " + websocket_accept(key).encode(), head)
            self.assertNotIn(PASSWORD.encode(), data)
            self.assertNotIn(b"rtsp://", data)
            self.assertNotIn(b"exec:bad", data)
            client.sendall(b"frame-from-browser")
            echoed = rest
            while b"frame-from-browser" not in echoed:
                chunk = client.recv(4096)
                self.assertTrue(chunk)
                echoed += chunk
            self.assertIn(b"frame-from-browser", echoed)
        finally:
            client.close()
        upstream = b"".join(self.seen)
        self.assertIn(f"GET /api/ws?src={self.module['id']} HTTP/1.1".encode(), upstream)
        self.assertNotIn(b"exec:bad", upstream)
        self.assertNotIn(b"/api/streams HTTP", upstream.split(b"GET /api/ws", 1)[1] if b"GET /api/ws" in upstream else b"")
        self.assertIn(b"PATCH /api/streams?", upstream)
        self.assertNotIn(PASSWORD.encode(), upstream)
        patch = next(item for item in self.seen if item.startswith(b"PATCH "))
        target = patch.split(b" ", 2)[1].decode()
        source = parse_qs(urlsplit(target).query)["src"][0]
        self.assertEqual(source, "rtsp://cam%40era:p%40ss%2Fword@192.168.1.45:554/stream2#transport=tcp")

    def test_direct_proxy_uses_server_chosen_path(self):
        # Jednotkový průchod duplexu mimo HTTP server, stále bez hesla v odpovědi.
        key = base64.b64encode(os.urandom(16)).decode()
        left, right = socket.socketpair()
        worker = threading.Thread(target=proxy_websocket, args=(right, key, self.module["id"]), daemon=True)
        worker.start()
        try:
            data = b""
            left.settimeout(3)
            while b"\r\n\r\n" not in data:
                data += left.recv(4096)
            self.assertIn(websocket_accept(key).encode(), data)
            self.assertNotIn(PASSWORD.encode(), data)
            left.sendall(b"ping")
            echoed = b""
            while b"ping" not in echoed:
                echoed += left.recv(4096)
            self.assertEqual(echoed, b"ping")
        finally:
            left.close()
            right.close()
            worker.join(timeout=2)
        paths = [item.split(b"\r\n", 1)[0] for item in self.seen]
        self.assertIn(f"GET /api/ws?src={self.module['id']} HTTP/1.1".encode(), paths)


if __name__ == "__main__":
    unittest.main()
