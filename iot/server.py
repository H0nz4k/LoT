#!/usr/bin/env python3
"""Samostatná IoT služba HanzHub: web, API a lokální TinyTuya ovladače."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request

from iot import IoTManager, PRIVATE_NETWORKS
from iot_driver import DRIVERS, IoTError

WEB_DIR = Path(os.environ.get("HANZHUB_IOT_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/iot.html": ("index.html", "text/html; charset=utf-8"),
          "/iot.css": ("iot.css", "text/css; charset=utf-8"),
          "/iot.js": ("iot.js", "text/javascript; charset=utf-8"),
          "/hanzlogo.svg": ("hanzlogo.svg", "image/svg+xml"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/version.json": ("version.json", "application/json")}


def internal_ip(value):
    try:
        address = ip_address(value)
        return address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def allowed_request(handler):
    peer = handler.client_address[0]
    if not internal_ip(peer):
        return False
    # Pouze místní reverse proxy smí předat původní adresu klienta.
    forwarded = handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For")
    if forwarded and not internal_ip(forwarded.split(",")[0].strip()):
        return False
    origin = handler.headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https") or parsed.netloc != handler.headers.get("Host", ""):
            return False
    return True


class Handler(BaseHTTPRequestHandler):
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
        if method == "GET" and path == "/api/iot/info":
            self._json({"dashboard_url": os.environ.get("HANZHUB_DASHBOARD_URL", "http://192.168.1.3:4001"),
                        "service": "hanzhub_iot"})
        elif method == "GET" and path == "/api/iot/health":
            self._json({"ok": True, "service": "hanzhub_iot", "port": self.server.server_port})
        elif method == "GET" and path == "/api/iot/devices":
            self._json({"devices": manager.list_modules(), "drivers": DRIVERS})
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
            match = re.fullmatch(r"/api/iot/devices/([a-f0-9]{12})(?:/(state|command))?", path)
            if not match:
                raise IoTError("Endpoint nenalezen.", 404, "not_found")
            module_id, action = match.groups()
            if method == "GET" and action == "state":
                self._json({"state": manager.state(module_id, fresh=query.get("fresh") == ["1"])})
            elif method == "GET" and action is None:
                self._json({"module": manager.get_module(module_id)})
            elif method == "POST" and action == "command":
                self._json({"ok": True, "state": manager.command(module_id, self._body())})
            elif method == "PUT" and action is None:
                self._json({"ok": True, "module": manager.update_module(module_id, self._body())})
            elif method == "DELETE" and action is None:
                manager.delete_module(module_id)
                self._json({"ok": True})
            else:
                raise IoTError("Metoda není podporovaná.", 405, "method_not_allowed")

    def _dispatch(self, method):
        path = urlsplit(self.path).path
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
        if not allowed_request(self):
            self._json({"error": "Ovládání a správa IoT jsou dostupné z domácí sítě.",
                        "code": "lan_only"}, 403)
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
    """Přidá kartu pouze pokud chybí; zachová ruční úpravy i ostatní služby."""
    base = os.environ.get("HANZHUB_DASHBOARD_API", "http://127.0.0.1:4010").rstrip("/")
    port = int(os.environ.get("HANZHUB_IOT_PORT", "4011"))
    service = {"key": "iot", "name": "IoT moduly", "desc": "Ovládání a správa chytrých zařízení",
               "url": os.environ.get("HANZHUB_IOT_URL", f"http://192.168.1.3:{port}"),
               "ping": f"http://127.0.0.1:{port}/api/iot/health",
               "icon": "icons/iot.svg", "color": "linear-gradient(180deg,#fb923c,#f59e0b)", "public": False}
    for _ in range(12):
        try:
            with urllib.request.urlopen(base + "/api/services", timeout=3) as response:
                services = json.load(response).get("services", [])
            if any(item.get("key") == "iot" for item in services):
                return
            request = urllib.request.Request(base + "/api/services", method="POST",
                data=json.dumps(service).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=3):
                print("IoT karta přidaná do dashboardu.", flush=True)
                return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(5)
    print("Automatické přidání IoT karty se nepodařilo. Použijte instalaci integrace dashboardu.", flush=True)


def make_server(host="0.0.0.0", port=4011, manager=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.manager = manager or IoTManager()
    return server


if __name__ == "__main__":
    port = int(os.environ.get("HANZHUB_IOT_PORT", "4011"))
    server = make_server(port=port)
    threading.Thread(target=register_dashboard, daemon=True).start()
    print(f"HanzHub IoT: 0.0.0.0:{port}", flush=True)
    server.serve_forever()
