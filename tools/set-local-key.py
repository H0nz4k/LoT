#!/usr/bin/env python3
"""Enter a local key once, without command-line secrets or network requests."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "iot"))
from local_keys import KeyFileError, key_path, validate_key


def save_key(directory, device_id, key):
    target = key_path(directory, device_id)
    encoded = validate_key(key.encode("utf-8")).encode("ascii")
    directory = Path(directory)
    if directory.is_symlink():
        raise KeyFileError("Adresář klíčů nesmí být symbolický odkaz.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and directory.stat().st_mode & 0o077:
        raise KeyFileError("Adresář klíčů musí mít oprávnění 700.")
    fd, temporary = tempfile.mkstemp(prefix=".key-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--device-id", required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Klíč zadávejte interaktivně v terminálu, nikoli přes argument nebo pipe.")
    try:
        key = getpass.getpass("Aktuální lokální klíč panelu: ")
        if key != getpass.getpass("Znovu pro kontrolu: "):
            raise KeyFileError("Zadané klíče se neshodují.")
        save_key(args.directory, args.device_id, key)
    except (OSError, ValueError):
        print("Klíč nebyl uložen. Ověřte 16 znaků, shodu zadání a oprávnění adresáře.", file=sys.stderr)
        return 1
    print("Klíč uložen; další nové spojení jej načte automaticky.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
