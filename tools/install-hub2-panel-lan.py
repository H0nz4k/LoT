#!/usr/bin/env python3
"""Install the versioned LAN-key overlay on HUB2; preserve all other live code."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path("/opt/hanzhub-iot/iot/iot_driver.py")
COMPONENT = Path("/opt/hanzhub/iot-local-lan")
KEYS = Path("/var/lib/hanzhub/iot/keys")
DROPIN = Path("/etc/systemd/system/hanzhub-iot.service.d/47-local-key-file.conf")


def patched_driver(source):
    if "from local_keys import KeyFileError, local_key" in source:
        ast.parse(source)
        return source
    old = '            device = factory(module["device_id"], module["ip"], module["local_key"],'
    if source.count(old) != 1:
        raise ValueError("Unrecognized driver; refusing to overwrite live code")
    replacement = '''            key = local_key(module)
        except KeyFileError as error:
            raise IoTError(str(error), 503, "local_key_unavailable") from None
        try:
            device = factory(module["device_id"], module["ip"], key,'''
    source = source.replace(old, replacement)
    header = "from datetime import datetime, timezone\n"
    if source.count(header) != 1:
        raise ValueError("Unrecognized imports")
    source = source.replace(header, header + "from local_keys import KeyFileError, local_key\n")
    ast.parse(source)
    return source


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expect-driver-sha", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo on the verified HUB2 host")
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_sha):
        parser.error("Expected full source commit SHA")
    if digest(DRIVER) != args.expect_driver_sha:
        parser.error("Live driver changed since inspection; no files changed")
    unit = subprocess.check_output(["systemctl", "show", "hanzhub-iot", "-p", "ExecStart", "--value"], text=True)
    if "/opt/hanzhub/hub2/iot-web.py" not in unit:
        parser.error("This installer is only for the discovered HUB2 systemd runtime")
    driver_text = patched_driver(DRIVER.read_text())
    version = json.loads((ROOT / "web/version.json").read_text())["version"]
    # Validate every Python source before the first live write.
    for name in ("iot/local_keys.py", "tools/set-local-key.py", "tools/verify-local-lan.py"):
        ast.parse((ROOT / name).read_text())
    account = pwd.getpwnam("hanz")
    files = {
        DRIVER: driver_text,
        DRIVER.with_name("local_keys.py"): (ROOT / "iot/local_keys.py").read_text(),
        COMPONENT / "iot/local_keys.py": (ROOT / "iot/local_keys.py").read_text(),
        COMPONENT / "tools/set-local-key.py": (ROOT / "tools/set-local-key.py").read_text(),
        COMPONENT / "tools/verify-local-lan.py": (ROOT / "tools/verify-local-lan.py").read_text(),
        Path("/opt/hanzhub-iot/web/version.json"): json.dumps({"version": version}) + "\n",
        DROPIN: "[Service]\nEnvironment=HANZHUB_TUYA_KEY_DIR=" + str(KEYS) +
            "\nExecStartPre=/opt/hanzhub/iot-venv/bin/python -B " + str(COMPONENT / "tools/verify-local-lan.py") +
            " " + str(COMPONENT / "manifest.json") + "\n",
    }
    manifest_path = COMPONENT / "manifest.json"
    backup = Path("/opt/hanzhub/.rollback-panel-lan-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    backup.mkdir(mode=0o700)
    restore = ["#!/bin/sh", "set -eu"]
    for index, target in enumerate([*files, manifest_path]):
        if target.is_symlink():
            raise ValueError("Unexpected symlink: " + str(target))
        if target.exists():
            saved = backup / str(index)
            shutil.copy2(target, saved)
            restore.append("cp -p '" + str(saved) + "' '" + str(target) + "'")
        else:
            restore.append("rm -f '" + str(target) + "'")
    restore += ["systemctl daemon-reload", "systemctl restart hanzhub-iot"]
    (backup / "restore.sh").write_text("\n".join(restore) + "\n")
    os.chmod(backup / "restore.sh", 0o700)
    print("Rollback: sudo sh " + str(backup / "restore.sh"), flush=True)
    try:
        KEYS.mkdir(mode=0o700, parents=True, exist_ok=True)
        if KEYS.is_symlink():
            raise ValueError("Key directory cannot be a symlink")
        os.chown(KEYS, account.pw_uid, account.pw_gid)
        os.chmod(KEYS, 0o700)
        for target, content in files.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            os.chmod(target, 0o644)
        manifest = {"version": version, "repository": "https://github.com/H0nz4k/LoT",
                    "source_sha": args.source_sha, "key_directory": str(KEYS),
                    "files": {str(p): digest(p) for p in files}, "rollback": str(backup / "restore.sh")}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        os.chmod(manifest_path, 0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", "hanzhub-iot"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", "hanzhub-iot"], check=True)
    except Exception:
        subprocess.run(["sh", str(backup / "restore.sh")], check=True)
        raise
    print("Installed LoT LAN overlay " + version + "; secret files preserved; enter the panel key separately.")


if __name__ == "__main__":
    main()
