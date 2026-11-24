"""Local Tuya secrets; never expose contents through HTTP or diagnostics."""
import os
from pathlib import Path
import re
import stat


class KeyFileError(ValueError):
    pass


def key_path(directory, device_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
        raise KeyFileError("Neplatné ID zařízení pro soubor klíče.")
    return Path(directory) / (device_id + ".key")


def validate_key(raw):
    raw = raw.rstrip(b"\r\n")
    if len(raw) != 16 or any(byte < 32 or byte > 126 for byte in raw):
        raise KeyFileError("Lokální klíč musí obsahovat 16 tisknutelných ASCII znaků.")
    return raw.decode("ascii")


def local_key(module):
    directory = os.environ.get("HANZHUB_TUYA_KEY_DIR")
    if not directory:
        return module["local_key"]
    # With an explicit directory, do not silently use an old database key.
    path = key_path(directory, module["device_id"])
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
                raise KeyFileError("Soubor klíče musí být běžný soubor s oprávněním 600.")
            return validate_key(stream.read(64))
    except OSError:
        raise KeyFileError("Soubor lokálního klíče chybí nebo není čitelný.") from None
