"""Registr modulů a jejich lokální ovládání. Klíče nejsou součástí veřejného API."""
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from iot_driver import DRIVERS, IoTError, LocalTuya, check_bool, check_int, utc_now, validate_command

PANEL_MAC = "fc:3c:d7:4c:a2:dc"
PRIVATE_NETWORKS = tuple(IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PUBLIC_FIELDS = ("id", "name", "room", "driver", "device_id", "mac", "ip", "version",
                 "power_dp", "enabled", "created_at", "updated_at")
EDIT_FIELDS = {"name", "room", "driver", "device_id", "mac", "ip", "version", "power_dp",
               "enabled", "local_key", "source_id"}


def normalize_mac(value):
    value = str(value or "").lower().replace("-", ":")
    if value and not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
        raise IoTError("MAC adresa není platná.")
    return value or None


def local_address(value):
    try:
        address = IPv4Address(value)
    except (ValueError, TypeError):
        raise IoTError("Zadejte platnou LAN IPv4 adresu.") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise IoTError("Modul musí mít adresu v místní síti (10.x, 172.16–31.x nebo 192.168.x).")
    return str(address)


def text_value(value, label, limit, required=False):
    if not isinstance(value, str):
        raise IoTError(f"{label}: požadován text.")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise IoTError(f"{label}: vyplňte nejvýše {limit} znaků.")
    return value


class IoTManager:
    def __init__(self, data_dir=None, devices_file=None, adapter=None, cache_seconds=8):
        self.data_dir = Path(data_dir or os.environ.get("HANZHUB_IOT_DATA_DIR", "/var/lib/hanzhub-iot"))
        self.devices_file = Path(devices_file or os.environ.get(
            "HANZHUB_IOT_DEVICES_FILE", "/run/hanzhub-iot/devices.json"))
        self.adapter = adapter or LocalTuya()
        self.cache_seconds = cache_seconds
        self._cache = {}
        self._locks = {}
        self._guard = threading.RLock()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.data_dir, 0o700)
            self.db_path = self.data_dir / "modules.sqlite3"
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS modules (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, room TEXT NOT NULL,
                        driver TEXT NOT NULL, device_id TEXT NOT NULL UNIQUE,
                        mac TEXT UNIQUE, ip TEXT NOT NULL, version TEXT NOT NULL,
                        local_key TEXT NOT NULL, power_dp INTEGER NOT NULL,
                        enabled INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, module_id TEXT NOT NULL,
                        control TEXT NOT NULL, value TEXT NOT NULL, ok INTEGER NOT NULL,
                        message TEXT NOT NULL, at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
            os.chmod(self.db_path, 0o600)
        except OSError:
            raise IoTError("Nelze otevřít úložiště IoT modulů.", 503, "storage_unavailable") from None
        self._bootstrap()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _module(self, module_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
        if row is None:
            raise IoTError("Modul nenalezen.", 404, "not_found")
        module = dict(row)
        module["enabled"] = bool(module["enabled"])
        return module

    @contextmanager
    def _device_lock(self, module_id):
        with self._guard:
            lock = self._locks.setdefault(module_id, threading.Lock())
        if not lock.acquire(timeout=3):
            raise IoTError("Probíhá jiný příkaz pro tento modul. Zkuste to znovu.", 409, "busy")
        try:
            yield
        finally:
            lock.release()

    def _public(self, module):
        public = {k: module[k] for k in PUBLIC_FIELDS}
        public["has_key"] = bool(module["local_key"])
        public["capabilities"] = DRIVERS[module["driver"]]
        with self._guard:
            cached = self._cache.get(module["id"])
            public["state"] = dict(cached[1]) if cached and time.monotonic() - cached[0] < self.cache_seconds else None
        return public

    def list_modules(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM modules ORDER BY created_at, id").fetchall()
        modules = []
        for row in rows:
            module = dict(row)
            module["enabled"] = bool(module["enabled"])
            modules.append(self._public(module))
        return modules

    def get_module(self, module_id):
        return self._public(self._module(module_id))

    def _source(self):
        try:
            rows = json.loads(self.devices_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise IoTError("Nelze načíst TinyTuya devices.json. Ověřte připojení souboru do API.",
                           503, "import_unavailable") from None
        if not isinstance(rows, list):
            raise IoTError("TinyTuya devices.json musí obsahovat seznam zařízení.")
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    def candidates(self):
        with self._db() as db:
            registered = {r["device_id"] for r in db.execute("SELECT device_id FROM modules")}
        candidates = []
        for row in self._source():
            try:
                mac = normalize_mac(row.get("mac"))
            except IoTError:
                mac = None
            candidates.append({
                "source_id": row["id"], "name": row.get("name") or "Tuya modul",
                "ip": row.get("ip") or "", "mac": mac,
                "model": row.get("model") or "", "has_key": bool(row.get("key")),
                "registered": row["id"] in registered,
                "driver_hint": "bot_iph2" if mac == PANEL_MAC else "tuya_switch",
            })
        return candidates

    def _validated(self, data, current=None):
        if not isinstance(data, dict) or set(data) - EDIT_FIELDS:
            raise IoTError("Neplatná pole konfigurace modulu.")
        merged = {"name": "", "room": "", "driver": "bot_iph2", "version": "3.4",
                  "power_dp": 1, "enabled": True, "mac": None}
        if current:
            merged.update(current)
        if data.get("source_id"):
            source = next((r for r in self._source() if r["id"] == data["source_id"]), None)
            if source is None:
                raise IoTError("Vybrané zařízení není v TinyTuya souboru.", 404, "source_not_found")
            merged.update({"device_id": source["id"], "local_key": source.get("key", ""),
                           "mac": source.get("mac"), "ip": source.get("ip") or "",
                           "name": source.get("name") or "Tuya modul"})
        merged.update({k: v for k, v in data.items() if k != "source_id"})
        if current and data.get("local_key") == "":
            merged["local_key"] = current["local_key"]
        merged["name"] = text_value(merged["name"], "Název", 80, True)
        merged["room"] = text_value(merged["room"], "Místnost", 80)
        merged["device_id"] = text_value(merged.get("device_id", ""), "ID zařízení", 64, True)
        if not isinstance(merged["driver"], str) or merged["driver"] not in DRIVERS:
            raise IoTError("Zvolený typ modulu není podporovaný.")
        merged["mac"] = normalize_mac(merged["mac"])
        merged["ip"] = local_address(merged.get("ip", ""))
        if merged["version"] not in ("3.1", "3.2", "3.3", "3.4", "3.5"):
            raise IoTError("Nepodporovaná verze Tuya protokolu.")
        key = merged.get("local_key", "")
        if not isinstance(key, str) or len(key.encode("utf-8")) != 16:
            raise IoTError("Lokální klíč musí mít 16 bytů. Načtěte jej přes TinyTuya wizard.")
        merged["power_dp"] = check_int(merged["power_dp"], 1, 255)
        if merged["driver"] == "bot_iph2":
            merged["power_dp"] = 1
        merged["enabled"] = check_bool(merged["enabled"])
        return merged

    def add_module(self, data):
        module = self._validated(data)
        module_id = uuid.uuid4().hex[:12]
        now = utc_now()
        values = {**module, "id": module_id, "created_at": now, "updated_at": now}
        columns = (*PUBLIC_FIELDS, "local_key")
        try:
            with self._db() as db:
                count = db.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
                if count >= 100:
                    raise IoTError("Registr obsahuje nejvýše 100 modulů.")
                db.execute(f"INSERT INTO modules ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(values[k] for k in columns))
        except sqlite3.IntegrityError:
            raise IoTError("Zařízení s tímto ID nebo MAC už je přidané.", 409, "duplicate") from None
        return self.get_module(module_id)

    def update_module(self, module_id, data):
        with self._device_lock(module_id):
            current = self._module(module_id)
            module = self._validated(data, current)
            module["updated_at"] = utc_now()
            columns = ("name", "room", "driver", "device_id", "mac", "ip", "version", "power_dp",
                       "enabled", "local_key", "updated_at")
            try:
                with self._db() as db:
                    db.execute(f"UPDATE modules SET {','.join(k + ' = ?' for k in columns)} WHERE id = ?",
                               tuple(module[k] for k in columns) + (module_id,))
            except sqlite3.IntegrityError:
                raise IoTError("Zařízení s tímto ID nebo MAC už je přidané.", 409, "duplicate") from None
            with self._guard:
                self._cache.pop(module_id, None)
        return self.get_module(module_id)

    def delete_module(self, module_id):
        with self._device_lock(module_id):
            self._module(module_id)
            with self._db() as db:
                db.execute("DELETE FROM modules WHERE id = ?", (module_id,))
                db.execute("DELETE FROM events WHERE module_id = ?", (module_id,))
            with self._guard:
                self._cache.pop(module_id, None)

    def _bootstrap(self):
        with self._db() as db:
            initialized = db.execute("SELECT value FROM meta WHERE key = 'initialized'").fetchone()
        if initialized:
            return
        try:
            self.import_source()
        except IoTError:
            return
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('initialized','1')")

    def import_source(self):
        """Obnoví klíče u známých MAC a přidá ověřený panel. Bez cloudových volání."""
        rows = self._source()
        added, updated, skipped = 0, 0, 0
        for row in rows:
            try:
                mac = normalize_mac(row.get("mac"))
                with self._db() as db:
                    existing = db.execute("SELECT id FROM modules WHERE device_id = ? OR (mac IS NOT NULL AND mac = ?)",
                                          (row["id"], mac)).fetchone()
                if existing:
                    # Zachová ručně zvolenou IP a uživatelské názvy; obnoví identitu a klíč po přepárování.
                    self.update_module(existing["id"], {"device_id": row["id"], "local_key": row.get("key", "")})
                    updated += 1
                elif mac == PANEL_MAC:
                    self.add_module({"source_id": row["id"], "name": "Infrapanel BOT", "room": "",
                                     "driver": "bot_iph2", "ip": row.get("ip") or "192.168.1.102"})
                    added += 1
                else:
                    skipped += 1
            except IoTError:
                skipped += 1
        return {"added": added, "updated": updated, "skipped": skipped}

    def state(self, module_id, fresh=False):
        module = self._module(module_id)
        if not module["enabled"]:
            return {"online": False, "paused": True, "checked_at": utc_now()}
        with self._guard:
            cached = self._cache.get(module_id)
            if cached and not fresh and time.monotonic() - cached[0] < self.cache_seconds:
                return dict(cached[1])
        with self._device_lock(module_id):
            module = self._module(module_id)
            if not module["enabled"]:
                return {"online": False, "paused": True, "checked_at": utc_now()}
            with self._guard:
                cached = self._cache.get(module_id)
                if cached and not fresh and time.monotonic() - cached[0] < self.cache_seconds:
                    return dict(cached[1])
            try:
                state = self.adapter.status(module)
            except IoTError as error:
                state = {"online": False, "error": str(error), "error_code": error.code}
            state["checked_at"] = utc_now()
            with self._guard:
                self._cache[module_id] = (time.monotonic(), state)
            return dict(state)

    def command(self, module_id, command):
        module = self._module(module_id)
        control, value = validate_command(module["driver"], command)
        with self._device_lock(module_id):
            module = self._module(module_id)
            if not module["enabled"]:
                raise IoTError("Modul je pozastavený. Povolte jej ve správě.", 409, "paused")
            # Typ se mohl změnit během čekání na zámek.
            validate_command(module["driver"], command)
            try:
                state = self.adapter.command(module, command)
            except IoTError as error:
                with self._guard:
                    self._cache.pop(module_id, None)
                self._event(module_id, control, value, False, str(error))
                raise
            state["checked_at"] = utc_now()
            with self._guard:
                self._cache[module_id] = (time.monotonic(), state)
            self._event(module_id, control, value, True, "Potvrzeno panelem")
            return state

    def _event(self, module_id, control, value, ok, message):
        with self._db() as db:
            db.execute("INSERT INTO events(module_id,control,value,ok,message,at) VALUES(?,?,?,?,?,?)",
                       (module_id, control, json.dumps(value), int(ok), message, utc_now()))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")

    def events(self, module_id=None):
        with self._db() as db:
            if module_id:
                self._module(module_id)
                rows = db.execute("SELECT * FROM events WHERE module_id = ? ORDER BY id DESC LIMIT 20",
                                  (module_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 20").fetchall()
        return [{**dict(row), "value": json.loads(row["value"]), "ok": bool(row["ok"])} for row in rows]

    def panel(self, module_id=None):
        modules = self.list_modules()
        if module_id:
            module = next((m for m in modules if m["id"] == module_id and m["driver"] == "bot_iph2"), None)
        else:
            module = next((m for m in modules if m["mac"] == PANEL_MAC and m["driver"] == "bot_iph2"), None)
            module = module or next((m for m in modules if m["driver"] == "bot_iph2"), None)
        if not module:
            raise IoTError("Žádný infrapanel není přidaný.", 404, "not_found")
        return {"module": module, "state": self.state(module["id"])}
