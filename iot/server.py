#!/usr/bin/env python3
"""Samostatná IoT služba HanzHub: web, API a lokální TinyTuya ovladače."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import base64
import json
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request

from iot import IoTManager, PRIVATE_NETWORKS
from iot_driver import DRIVERS, IoTError
from access import RemoteAccess, remote_route_allowed
from live import ensure_stream, live_upgrade_header, parse_request_head, proxy_websocket, websocket_key
import sys

WEB_DIR = Path(os.environ.get("HANZHUB_IOT_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/iot.html": ("index.html", "text/html; charset=utf-8"),
          "/iot.css": ("iot.css", "text/css; charset=utf-8"),
          "/iot.js": ("iot.js", "text/javascript; charset=utf-8"),
          "/hanzlogo.svg": ("hanzlogo.svg", "image/svg+xml"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/iot-icon.svg": ("iot-icon.svg", "image/svg+xml"),
          "/ewelink.js": ("ewelink.js", "text/javascript; charset=utf-8"),
          "/version.json": ("version.json", "application/json")}


def internal_ip(value):
    try:
        address = ip_address(value)
        return address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def request_access(handler):
    peer = handler.client_address[0]
    if not internal_ip(peer):
        return None
    access = handler.server.remote_access
    token = handler.headers.get("Cf-Access-Jwt-Assertion")
    # Pouze místní reverse proxy smí předat původní adresu klienta.
    forwarded = handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For")
    remote = bool(token or (forwarded and not internal_ip(forwarded.split(",")[0].strip()))
                  or (access.origin and handler.headers.get("Host") == urlsplit(access.origin).netloc))
    origin = handler.headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return None
        if parsed.scheme not in ("http", "https") or parsed.netloc != handler.headers.get("Host", ""):
            return None
        if remote and origin != access.origin:
            return None
    if remote:
        # Do not fall back to LAN if a forwarded identity/token is invalid.
        if not access.enabled or handler.headers.get("Host") != urlsplit(access.origin).netloc:
            return None
        return "remote" if access.verify(token) else None
    return "lan"


def allowed_request(handler):
    return request_access(handler) is not None


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        # WebSocket se nesmí číst přes buffered rfile: ten by spolkl rámce za hlavičkami.
        if self._take_live_websocket():
            return
        super().handle()

    def _peek_headers(self):
        previous = self.connection.gettimeout()
        self.connection.settimeout(3)
        try:
            data = b""
            for _ in range(20):
                try:
                    data = self.connection.recv(16384, socket.MSG_PEEK)
                except TimeoutError:
                    break
                if not data:
                    return b""
                if b"\r\n\r\n" in data:
                    return data.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                readable, _, _ = select.select([self.connection], [], [], 1)
                if not readable:
                    break
            return b""
        except OSError:
            return b""
        finally:
            self.connection.settimeout(previous)

    def _recv_exact(self, size):
        data = b""
        while len(data) < size:
            chunk = self.connection.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def _take_live_websocket(self):
        header = self._peek_headers()
        if not live_upgrade_header(header):
            return False
        if self._recv_exact(len(header)) != header:
            return True
        self._serve_live_socket(header)
        return True

    def _serve_live_socket(self, raw):
        self.request_version = "HTTP/1.1"
        self.command = "GET"
        try:
            method, target, version, headers = parse_request_head(raw)
        except IoTError as error:
            self._json({"error": str(error), "code": error.code}, error.status)
            return
        self.command = method
        self.path = target
        self.request_version = version
        self.headers = headers
        if not self._reject_if_unauthorized():
            return
        match = re.fullmatch(r"/api/iot/devices/([a-f0-9]{12})/live", urlsplit(self.path).path.rstrip("/"))
        if self.command != "GET" or not match:
            self._json({"error": "Endpoint nenalezen.", "code": "not_found"}, 404)
            return
        try:
            module = self.server.manager.camera_for_live(match.group(1))
            key = websocket_key(self.headers)
            ensure_stream(module)
            proxy_websocket(self.connection, key, module["id"])
        except IoTError as error:
            self._json({"error": str(error), "code": error.code}, error.status)
        except (sqlite3.Error, OSError):
            self._json({"error": "Úložiště modulů není dostupné.", "code": "storage_unavailable"}, 503)
        except Exception:
            self._json({"error": "Požadavek nelze dokončit.", "code": "internal_error"}, 500)

    def log_message(self, fmt, *args):
        # Never log OAuth query parameters or application secrets.
        sys.stderr.write(self.command + " " + urlsplit(self.path).path + "\n")

    def _json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise IoTError("Požadován JSON požadavek.", 415, "invalid_content_type")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError()
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise IoTError("Neplatný nebo příliš velký JSON požadavek.") from None
        if not isinstance(body, dict):
            raise IoTError("JSON musí být objekt.")
        return body

    def _route_api(self, method):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        manager = self.server.manager
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/api/iot/ewelink/status":
            self._json(manager.ewelink_adapter.client.status())
        elif method == "GET" and path == "/api/iot/ewelink/login":
            self._json({"url":manager.ewelink_adapter.client.login()})
        elif method == "GET" and path == "/api/iot/ewelink/devices":
            self._json({"devices":manager.ewelink_adapter.client.devices()})
        elif method == "POST" and path == "/api/iot/ewelink/import":
            body=self._body()
            if set(body)!={"device_id"}:raise IoTError("Vyber zařízení eWeLink.")
            candidate=next((x for x in manager.ewelink_adapter.client.devices() if x['device_id']==body['device_id']),None)
            if candidate is None:raise IoTError("Zařízení není v připojeném účtu.")
            self._json({"ok":True,"module":manager.add_module({"driver":"sonoff_ewelink","name":candidate['name'],"room":candidate['room'],"device_id":candidate['device_id']})},201)
        elif method == "GET" and path == "/api/iot/info":
            self._json({"dashboard_url": os.environ.get("HANZHUB_DASHBOARD_URL", "http://192.168.1.3:4001"),
                        "service": "hanzhub_iot", "access": self.access_mode,
                        "can_manage": self.access_mode == "lan"})
        elif method == "GET" and path == "/api/iot/health":
            self._json({"ok": True, "service": "hanzhub_iot", "port": self.server.server_port})
        elif method == "GET" and path == "/api/iot/devices":
            self._json({"devices": manager.list_modules(), "drivers": DRIVERS,
                        "access": self.access_mode, "can_manage": self.access_mode == "lan"})
        elif method == "GET" and path == "/api/iot/candidates":
            self._json({"candidates": manager.candidates()})
        elif method == "GET" and path == "/api/iot/panel":
            self._json(manager.panel(query.get("id", [None])[0]))
        elif method == "GET" and path == "/api/iot/events":
            self._json({"events": manager.events(query.get("id", [None])[0])})
        elif method == "POST" and path == "/api/iot/devices":
            self._json({"ok": True, "module": manager.add_module(self._body())}, 201)
        elif method == "POST" and path == "/api/iot/import":
            if self._body():
                raise IoTError("Import očekává prázdný objekt.")
            self._json({"ok": True, **manager.import_source()})
        else:
            match = re.fullmatch(
                r"/api/iot/devices/([a-f0-9]{12})(?:/(state|command|schedule|presets|snapshot|live)(?:/([a-f0-9]{12}))?)?",
                path)
            if not match:
                raise IoTError("Endpoint nenalezen.", 404, "not_found")
            module_id, action, extra = match.groups()
            if extra and action != "schedule":
                raise IoTError("Endpoint nenalezen.", 404, "not_found")
            if method == "GET" and action == "state":
                self._json({"state": manager.state(module_id, fresh=query.get("fresh") == ["1"])})
            elif method == "GET" and action == "live":
                # Bez hlavičky Upgrade se go2rtc nevolá. WebSocket vyřizuje handle().
                manager.camera_for_live(module_id)
                raise IoTError("Živé video vyžaduje WebSocket.", 426, "upgrade_required")
            elif method == "GET" and action == "snapshot":
                payload = manager.snapshot(module_id)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            elif method == "GET" and action is None:
                self._json({"module": manager.get_module(module_id)})
            elif method == "POST" and action == "command":
                self._json({"ok": True, "state": manager.command(module_id, self._body())})
            elif method == "PUT" and action is None:
                self._json({"ok": True, "module": manager.update_module(module_id, self._body())})
            elif method == "DELETE" and action is None:
                manager.delete_module(module_id)
                self._json({"ok": True})
            elif method == "GET" and action == "schedule" and not extra:
                self._json(manager.schedule_view(module_id))
            elif method == "POST" and action == "schedule" and not extra:
                self._json({"ok": True, "rule": manager.create_rule(module_id, self._body())}, 201)
            elif method == "PUT" and action == "schedule" and extra:
                self._json({"ok": True, "rule": manager.update_rule(module_id, extra, self._body())})
            elif method == "DELETE" and action == "schedule" and extra:
                manager.delete_rule(module_id, extra)
                self._json({"ok": True})
            elif method == "GET" and action == "presets":
                self._json(manager.presets(module_id))
            elif method == "PUT" and action == "presets":
                self._json({"ok": True, **manager.set_presets(module_id, self._body())})
            else:
                raise IoTError("Metoda není podporovaná.", 405, "method_not_allowed")

    def _reject_if_unauthorized(self):
        self.access_mode = request_access(self)
        if self.access_mode is None:
            self._json({"error": "Připoj se z domácí sítě nebo se přihlas přes Cloudflare Access. Vzdálené ovládání musí být povolené na serveru.",
                        "code": "access_required" if self.server.remote_access.enabled else "lan_only"}, 403)
            return False
        if self.access_mode == "remote" and not remote_route_allowed(self.command, urlsplit(self.path).path.rstrip("/")):
            self._json({"error": "Správa modulů je dostupná pouze z domácí sítě. Ovládání zařízení je povolené.",
                        "code": "management_lan_only"}, 403)
            return False
        return True

    def _dispatch(self, method):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/" and ("code" in query or "state" in query):
            if request_access(self) is None:
                self._json({"error":"Přihlas se k HanzHubu."},403)
                return
            try:
                self.server.manager.ewelink_adapter.client.callback(query)
                location="/?ewelink=connected"
            except IoTError:
                location="/?ewelink=error"
            self.send_response(303)
            self.send_header("Location",location)
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length","0")
            self.end_headers()
            return
        if not path.startswith("/api/iot"):
            if method != "GET" or path not in ASSETS:
                self._json({"error": "Stránka nenalezena."}, 404)
                return
            name, content_type = ASSETS[path]
            try:
                payload = (WEB_DIR / name).read_bytes()
            except OSError:
                self._json({"error": "Soubor stránky není dostupný."}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self._reject_if_unauthorized():
            return
        try:
            self._route_api(method)
        except IoTError as error:
            self._json({"error": str(error), "code": error.code}, error.status)
        except (sqlite3.Error, OSError):
            self._json({"error": "Úložiště modulů není dostupné.", "code": "storage_unavailable"}, 503)
        except Exception:
            # Raw výjimka může obsahovat privátní konfiguraci; neposílá se klientovi.
            self._json({"error": "Požadavek nelze dokončit.", "code": "internal_error"}, 500)

    def do_GET(self):
        # A credential-free, read-only availability probe for the public IoT page.
        # CORS is granted only here, never for device data or management APIs.
        if urlsplit(self.path).path == "/api/iot/lan-check":
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            direct = (internal_ip(self.client_address[0])
                      and host in ("192.168.1.3:4011", "127.0.0.1:4011", "localhost:4011")
                      and not any(self.headers.get(h) for h in
                                  ("CF-Connecting-IP", "X-Forwarded-For", "Cf-Access-Jwt-Assertion")))
            if not direct or origin not in ("", "https://iot.hanzhub.cz"):
                self._json({"error": "Local probe only."}, 403)
                return
            payload = b'{"service":"hanzhub_iot","lan":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(payload)
            return
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        if not allowed_request(self):
            self._json({"error": "Přístup pouze z domácí sítě."}, 403)
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def register_dashboard():
    """Přidá kartu nebo obnoví její ikonu; ostatní ruční nastavení zachová."""
    base = os.environ.get("HANZHUB_DASHBOARD_API", "http://127.0.0.1:4010").rstrip("/")
    port = int(os.environ.get("HANZHUB_IOT_PORT", "4011"))
    icon = "data:image/svg+xml;base64," + base64.b64encode((WEB_DIR / "iot-icon.svg").read_bytes()).decode("ascii")
    service = {"key": "iot", "name": "IoT moduly", "desc": "Ovládání a správa chytrých zařízení",
               "url": os.environ.get("HANZHUB_IOT_URL", f"http://192.168.1.3:{port}"),
               "ping": f"http://127.0.0.1:{port}/api/iot/health",
               "icon": icon, "color": "linear-gradient(180deg,#fb923c,#f59e0b)", "public": False}
    for _ in range(12):
        try:
            with urllib.request.urlopen(base + "/api/services", timeout=3) as response:
                services = json.load(response).get("services", [])
            existing = next((item for item in services if item.get("key") == "iot"), None)
            if existing:
                if existing.get("icon") != icon:
                    request = urllib.request.Request(base + "/api/services/iot", method="PUT",
                        data=json.dumps({"icon": icon}).encode("utf-8"), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=3):
                        print("Ikona IoT karty obnovena.", flush=True)
                return
            request = urllib.request.Request(base + "/api/services", method="POST",
                data=json.dumps(service).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=3):
                print("IoT karta přidaná do dashboardu.", flush=True)
                return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(5)
    print("Automatické přidání IoT karty se nepodařilo. Použijte instalaci integrace dashboardu.", flush=True)


def make_server(host="0.0.0.0", port=4011, manager=None, remote_access=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.manager = manager or IoTManager()
    server.remote_access = remote_access if remote_access is not None else RemoteAccess.from_env()
    return server


if __name__ == "__main__":
    port = int(os.environ.get("HANZHUB_IOT_PORT", "4011"))
    server = make_server(port=port)
    server.manager.start_scheduler()
    threading.Thread(target=register_dashboard, daemon=True).start()
    print(f"HanzHub IoT: 0.0.0.0:{port}", flush=True)
    server.serve_forever()
