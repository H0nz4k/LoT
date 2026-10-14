"""eWeLink OAuth v2; no unofficial LAN endpoints or embedded app credentials.

Protocol: CoolKit-Technologies/eWeLink-API/en/{OAuth2.0,UIIDProtocol}.md.
Only an observed documented single switch shape is writable. Meter units for
new S60 UIIDs are not documented there, so they are deliberately not inferred.
"""
import json
import math
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from iot_driver import IoTError, utc_now, validate_command

HOSTS = {"eu": "https://eu-apia.coolkit.cc", "us": "https://us-apia.coolkit.cc",
         "as": "https://as-apia.coolkit.cc", "cn": "https://cn-apia.coolkit.cn"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to another host.


class CloudSonoff:
    def __init__(self, credentials_file=None, transport=None, clock=time.monotonic, sleeper=time.sleep):
        self.credentials_file = Path(credentials_file or os.environ.get(
            "HANZHUB_EWELINK_CREDENTIALS_FILE", "/var/lib/hanzhub-iot/ewelink.json"))
        self.transport = transport or build_opener(NoRedirect()).open
        self.clock, self.sleeper = clock, sleeper
        self.lock = threading.RLock()
        self.last_request = -float("inf")
        self.inventory = None
        self.next_inventory = 0
        self.credentials_id = None
        self.inventory_at = None

    @contextmanager
    def _session(self):
        if not self.lock.acquire(timeout=2):
            raise IoTError("Probíhá jiné cloudové ovládání. Zkuste to znovu.", 409, "busy")
        try:
            yield
        finally:
            self.lock.release()

    def _credentials(self):
        try:
            config = json.loads(self.credentials_file.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or config.get("region") not in HOSTS:
                raise ValueError()
            for key in ("appid", "access_token"):
                if (not isinstance(config.get(key), str) or not 1 <= len(config[key]) <= 4096
                        or any(ord(c) < 33 or ord(c) > 126 for c in config[key])):
                    raise ValueError()
            return config
        except (OSError, ValueError, TypeError):
            raise IoTError("Doplňte soukromý eWeLink OAuth soubor na serveru (region, appid, access_token).",
                           503, "credentials_missing") from None

    def _request(self, config, path, body=None):
        # One shared adapter serializes requests across devices, including writes.
        delay = 1 - (self.clock() - self.last_request)
        if delay > 0:
            self.sleeper(delay)
        self.last_request = self.clock()
        request = Request(HOSTS[config["region"]] + path,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + config["access_token"],
                                   "X-CK-Appid": config["appid"], "Content-Type": "application/json"},
                          method="GET" if body is None else "POST")
        try:
            with self.transport(request, timeout=4) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get("error")) is not int:
                raise ValueError()
        except Exception:
            raise IoTError("eWeLink cloud není dostupný; ověřte přístup, OAuth token a oprávnění aplikace.",
                           502, "cloud_unavailable") from None
        if result["error"] != 0:
            raise IoTError("eWeLink odmítl požadavek; ověřte OAuth token, kvótu a oprávnění aplikace.",
                           502, "cloud_rejected")
        if not isinstance(result.get("data"), dict):
            raise IoTError("Neplatná odpověď eWeLink.", 502, "invalid_response")
        return result["data"]

    def _thing(self, module, config, force=False):
        identity = (config["region"], config["appid"], config["access_token"])
        if identity != self.credentials_id:
            self.inventory, self.next_inventory = None, 0
            self.credentials_id = identity
        if force:
            rows = self._request(config, "/v2/device/thing", {
                "thingList": [{"itemType": 1, "id": module["device_id"]}]}).get("thingList")
            observed_at = utc_now()
        else:
            if self.clock() >= self.next_inventory:
                self.inventory = None
                self.next_inventory = self.clock() + 120  # Includes failures: no rapid retry loop.
                self.inventory = self._request(config, "/v2/device/thing?num=0").get("thingList")
                self.inventory_at = utc_now()
            rows = self.inventory
            observed_at = self.inventory_at
        if not isinstance(rows, list):
            raise IoTError("Seznam eWeLink není dostupný. Obnovení proběhne do 2 minut.", 502, "cloud_unavailable")
        matches = [r.get("itemData") for r in rows if isinstance(r, dict) and r.get("itemType") == 1
                   and isinstance(r.get("itemData"), dict) and r["itemData"].get("deviceid") == module["device_id"]]
        if len(matches) != 1 or matches[0].get("productModel") != "S60TPF":
            raise IoTError("Účet neobsahuje vlastní SONOFF S60TPF s tímto ID; variantu Matter nepřidávejte tímto ovladačem.",
                           400, "wrong_device")
        return {**matches[0], "_cloud_fetched_at": observed_at}

    @staticmethod
    def _switch(thing):
        params = thing.get("params")
        if not isinstance(params, dict):
            raise IoTError("Chybí ověřitelný stav spínače eWeLink.", 502, "unsupported_protocol")
        if params.get("switch") in ("on", "off") and "switches" not in params:
            return "switch", params["switch"] == "on"
        rows = params.get("switches")
        if ("switch" not in params and isinstance(rows, list) and len(rows) == 1
                and isinstance(rows[0], dict) and type(rows[0].get("outlet")) is int
                and rows[0]["outlet"] == 0 and rows[0].get("switch") in ("on", "off")):
            return "switches", rows[0]["switch"] == "on"
        raise IoTError("Nepodporovaný stav spínače; příkaz se neodešle.", 502, "unsupported_protocol")

    def _state(self, thing):
        shape, power = self._switch(thing)
        if type(thing.get("online")) is not bool:
            raise IoTError("Chybí stav připojení eWeLink.", 502, "invalid_response")
        state = {"online": thing["online"], "source": "ewelink_cloud", "model": "S60TPF",
                 "cloud_fetched_at": thing.get("_cloud_fetched_at"),
                 "energy_notice": "Jednotky měření tohoto S60TPF nejsou ověřené; spotřebu zobrazí eWeLink."}
        if thing["online"]:
            state["power"] = power
            # Only this published UIID has verified W/V/A units. Do not apply
            # its scaling to modern S60 UIIDs, nor mistake oneKwhData for today.
            extra = thing.get("extra") or {}
            if isinstance(extra, dict) and type(extra.get("uiid")) is int and extra["uiid"] == 5:
                for source, target in (("power", "power_w"), ("voltage", "voltage_v"), ("current", "current_a")):
                    raw = thing["params"].get(source)
                    try:
                        number = float(raw) if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) else float("nan")
                    except ValueError:
                        continue
                    if math.isfinite(number) and number >= 0:
                        state[target] = number
                state["energy_notice"] = "Údaje uložené v eWeLink cloudu · denní a měsíční kWh nejsou ověřené."
        return state

    def status(self, module):
        with self._session():
            return self._state(self._thing(module, self._credentials()))

    def command(self, module, command):
        _, value = validate_command("sonoff_s60tpf", command)
        with self._session():
            config = self._credentials()
            thing = self._thing(module, config, force=True)
            state = self._state(thing)
            if not state["online"]:
                raise IoTError("SONOFF je v eWeLink offline.", 503, "device_unavailable")
            shape, _ = self._switch(thing)
            params = {"switch": "on" if value else "off"}
            if shape == "switches":
                params = {"switches": [{"outlet": 0, **params}]}
            self.inventory, self.next_inventory = None, 0
            # A write timeout has an unknown outcome; never reuse pre-write data.
            self._request(config, "/v2/device/thing/status", {"type": 1, "id": module["device_id"], "params": params})
            state = self._state(self._thing(module, config, force=True))
            if not state["online"] or state.get("power") is not value:
                raise IoTError("eWeLink nepotvrdil požadovaný stav. Obnovte stav; příkaz neopakujeme automaticky.",
                               502, "unconfirmed")
            return state
