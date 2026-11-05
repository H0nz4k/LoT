"""Živé video kamery přes místní go2rtc. RTSP URL neopouští tento proces."""
from http.client import parse_headers
from ipaddress import IPv4Address, ip_address
import base64
import hashlib
import os
import re
import socket
import threading
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from iot import PRIVATE_NETWORKS
from iot_driver import IoTError

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_KEY = re.compile(r"[A-Za-z0-9+/]{22}==")
_LIVE_LINE = re.compile(r"GET /api/iot/devices/[a-f0-9]{12}/live(?:\?[^ ]*)? HTTP/1\.[01]")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)
_sources = {}
_sources_lock = threading.Lock()


def rtsp_source(module):
    """Sestaví RTSP URL substreamu. Volající ji nesmí logovat ani poslat klientovi."""
    try:
        address = IPv4Address(module["ip"])
    except (KeyError, ValueError, TypeError):
        raise IoTError("Kamera nemá platnou LAN adresu.", 502, "camera_unavailable") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise IoTError("Kamera musí být v místní síti.", 502, "camera_unavailable")
    username = module.get("tapo_username") or ""
    password = module.get("tapo_password") or ""
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        raise IoTError("U kamery chybí účet pro RTSP.", 502, "camera_unavailable")
    user = quote(username, safe="")
    secret = quote(password, safe="")
    # stream2 je u C320WS typicky H.264, takže ho zvládne MSE v prohlížeči.
    return f"rtsp://{user}:{secret}@{address}:554/stream2#transport=tcp"


def go2rtc_endpoint(base_url=None):
    raw = (base_url if base_url is not None else os.environ.get("HANZHUB_GO2RTC_URL", "")).strip()
    raw = raw or "http://127.0.0.1:1984"
    parsed = urlsplit(raw)
    host = parsed.hostname
    try:
        address = ip_address(host) if host else None
    except ValueError:
        address = None
    if (parsed.scheme != "http" or address is None or not address.is_loopback
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise IoTError("Živé video není nastavené.", 503, "live_misconfigured")
    return address.compressed, parsed.port or 80


def ensure_stream(module, opener=None):
    """Zaregistruje stream v go2rtc. PATCH nezapisuje heslo do konfiguračního souboru."""
    source = rtsp_source(module)
    name = module["id"]
    if not re.fullmatch(r"[a-f0-9]{12}", name):
        raise IoTError("Neplatná kamera.", 404, "not_found")
    host, port = go2rtc_endpoint()
    with _sources_lock:
        changed = name in _sources and _sources[name] != source
    if changed:
        _go2rtc(host, port, "DELETE", {"src": name}, opener, required=False)
    _go2rtc(host, port, "PATCH", {"name": name, "src": source}, opener, required=True)
    with _sources_lock:
        _sources[name] = source


def _go2rtc(host, port, method, query, opener, required):
    http_host = f"[{host}]" if ":" in host else host
    url = f"http://{http_host}:{port}/api/streams?{urlencode(query)}"
    request = Request(url, data=b"", method=method)
    call = opener or _OPENER.open
    try:
        with call(request, timeout=5) as response:
            response.read(65536)
    except HTTPError as error:
        error.read(65536)
        if required and error.code >= 400:
            raise IoTError("Živé video se nepodařilo připravit.", 502, "live_unavailable") from None
    except (OSError, URLError, TimeoutError):
        if required:
            raise IoTError("Živé video teď není dostupné.", 502, "live_unavailable") from None


def live_upgrade_header(raw):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 16384 or b"\r\n\r\n" not in raw:
        return False
    try:
        text = raw.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    except UnicodeError:
        return False
    lines = text.split("\r\n")
    if not lines or not _LIVE_LINE.fullmatch(lines[0]):
        return False
    fields = {}
    for line in lines[1:]:
        if ":" not in line:
            return False
        name, value = line.split(":", 1)
        fields.setdefault(name.strip().lower(), []).append(value.strip())
    upgrade = ",".join(fields.get("upgrade", [])).lower()
    connection = ",".join(fields.get("connection", [])).lower()
    return "websocket" in upgrade and "upgrade" in connection


def parse_request_head(raw):
    head = raw.split(b"\r\n\r\n", 1)[0]
    line, _, header_blob = head.partition(b"\r\n")
    try:
        method, target, version = line.decode("iso-8859-1").split(" ")
    except ValueError:
        raise IoTError("Neplatný požadavek.", 400, "bad_request") from None
    headers = parse_headers(BytesIO(header_blob + b"\r\n\r\n"))
    return method, target, version, headers


def websocket_key(headers):
    key = headers.get("Sec-WebSocket-Key", "")
    if not _WS_KEY.fullmatch(key) or headers.get("Sec-WebSocket-Version", "") != "13":
        raise IoTError("Prohlížeč neposlal platný WebSocket.", 426, "upgrade_required")
    return key


def websocket_accept(key):
    digest = hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def proxy_websocket(client, key, stream_name):
    """Převede jeden WebSocket na /api/ws?src=<id>. Cestu určuje server, ne prohlížeč."""
    if not re.fullmatch(r"[a-f0-9]{12}", stream_name):
        raise IoTError("Neplatná kamera.", 404, "not_found")
    host, port = go2rtc_endpoint()
    http_host = f"[{host}]" if ":" in host else host
    request = (
        f"GET /api/ws?src={stream_name} HTTP/1.1\r\n"
        f"Host: {http_host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        upstream = socket.create_connection((host, port), timeout=5)
    except OSError:
        raise IoTError("Živé video teď není dostupné.", 502, "live_unavailable") from None
    handed = False
    try:
        upstream.settimeout(5)
        upstream.sendall(request)
        head = _read_until(upstream, b"\r\n\r\n", 16384)
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0]:
            raise IoTError("Živé video teď není dostupné.", 502, "live_unavailable")
        accept = websocket_accept(key).encode("ascii")
        client.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n"
            b"\r\n" + rest
        )
        handed = True
        upstream.settimeout(None)
        client.settimeout(None)
        _duplex(client, upstream)
    except IoTError:
        raise
    except OSError:
        if not handed:
            raise IoTError("Živé video teď není dostupné.", 502, "live_unavailable") from None
    finally:
        try:
            upstream.close()
        except OSError:
            pass


def _read_until(sock, marker, limit):
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            break
    return data


def _duplex(left, right):
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    worker = threading.Thread(target=pump, args=(right, left), daemon=True)
    worker.start()
    pump(left, right)
    worker.join(timeout=2)
