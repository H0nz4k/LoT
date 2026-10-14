"""Interactive server-only provisioning; credentials never enter argv or logs."""
from getpass import getpass
import json
import os
from pathlib import Path
import tempfile
import sys

from sonoff_driver import CloudSonoff, HOSTS


def save_credentials(path, config):
    if not isinstance(config, dict) or config.get("region") not in HOSTS:
        raise ValueError("Neplatný region (eu/us/as/cn).")
    for key in ("appid", "access_token"):
        value = config.get(key)
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("APPID a OAuth token musí být vyplněné bez mezer či řídicích znaků.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".ewelink-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump({k: config[k] for k in ("region", "appid", "access_token")}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    if not sys.stdin.isatty():
        print("Spusťte tento nástroj v interaktivním terminálu (docker exec -it).")
        return 1
    try:
        config = {"region":input("Region eWeLink (eu/us/as/cn): ").strip(),
                  "appid":input("APPID schválené aplikace: ").strip(),
                  "access_token":getpass("OAuth accessToken (skrytý vstup): ")}
        save_credentials(CloudSonoff().credentials_file, config)
        print("OAuth přístup uložen soukromě na serveru. Restart pro obnovu tokenu není nutný.")
    except (ValueError, OSError, EOFError, KeyboardInterrupt):
        print("Přístup nebyl uložen. Ověřte vstupy a oprávnění úložiště.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
