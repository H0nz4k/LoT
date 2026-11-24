#!/usr/bin/env python3
"""Fail visibly if an update overwrote the installed LAN credential support."""
import hashlib
import json
import os
from pathlib import Path
import sys


def verify(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text())
    if os.environ.get("HANZHUB_TUYA_KEY_DIR") != manifest["key_directory"]:
        raise ValueError("Local key directory configuration changed")
    for name, expected in manifest["files"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("Installed LAN component changed: " + name)
    # The user supplies the secret separately. A missing key must not stop Tapo
    # or Sonoff: LocalTuya reports local_key_unavailable for the affected panel.


if __name__ == "__main__":
    try:
        verify(sys.argv[1])
    except (OSError, ValueError, KeyError, IndexError):
        sys.exit("LAN integrity check failed. Reapply the versioned LoT HUB2 installer; do not remove this check.")
