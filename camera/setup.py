#!/usr/bin/env python3
"""Generate local-only go2rtc config without committing secrets."""
import getpass
import os
from pathlib import Path
from urllib.parse import quote

base = Path(__file__).resolve().parent
runtime = base / "runtime"
runtime.mkdir(mode=0o700, exist_ok=True)
os.chmod(runtime, 0o700)
ip = input("LAN IP kamery: ").strip()
from ipaddress import IPv4Address, IPv4Network
address = IPv4Address(ip)
if not any(address in IPv4Network(n) for n in ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8")):
    raise SystemExit("Povolena je pouze LAN IP adresa.")
camera_user = input("Camera Account username: ").strip()
camera_pass = getpass.getpass("Camera Account password: ")
api_user = input("go2rtc viewer username: ").strip()
api_pass = getpass.getpass("go2rtc viewer password: ")
if not all((camera_user, camera_pass, api_user, api_pass)):
    raise SystemExit("Chybi povinne udaje.")
# YAML values use JSON string quoting (JSON scalar strings are accepted in YAML).
import json
source = f"rtsp://{quote(camera_user,safe='')}:{quote(camera_pass,safe='')}@{ip}:554/stream1"
config = (
    "api:\n"
    "  listen: \":1984\"\n"
    "  username: " + json.dumps(api_user) + "\n"
    "  password: " + json.dumps(api_pass) + "\n"
    "webrtc:\n"
    "  listen: \":8555\"\n"
    "streams:\n"
    "  tapo_c320ws:\n"
    "    - " + json.dumps(source) + "\n"
)
# Keep API secured by credentials; firewall ports 1984, 8555 to trusted LAN.
target = runtime / "go2rtc.yaml"
target.write_text(config, encoding="utf-8")
target.chmod(0o600)
environment = runtime / "camera.env"
environment.write_text("CAMERA_RTSP=" + source + "\n", encoding="utf-8")
environment.chmod(0o600)
print("Konfigurace vytvořena. Omez porty 1984 a 8555 na LAN.")
