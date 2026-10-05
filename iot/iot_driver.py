"""Ověřený BOT IPH2 a konfigurovatelný spínač přes lokální Tuya protokol."""
from datetime import datetime, timezone
import time


class IoTError(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


DRIVERS = {
    "bot_iph2": {
        "name": "Infrapanel BOT IPH2",
        "kind": "heater",
        "controls": ["power", "target_temp_c", "locked", "timer_minutes"],
        "temperature": {"min": 0, "max": 37, "step": 1, "unit": "°C"},
        "timer": {"min": 0, "max": 1440, "step": 60, "unit": "min"},
    },
    "tuya_switch": {
        "name": "Spínač / zásuvka Tuya",
        "kind": "switch",
        "controls": ["power"],
    },
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def check_bool(value):
    if type(value) is not bool:
        raise IoTError("Hodnota musí být true nebo false.")
    return value


def check_int(value, low, high, step=1):
    if type(value) is not int or not low <= value <= high or value % step:
        raise IoTError(f"Požadováno celé číslo {low}–{high}, krok {step}.")
    return value


def validate_command(driver, command):
    if not isinstance(command, dict) or set(command) != {"control", "value"}:
        raise IoTError("Příkaz musí obsahovat právě control a value.")
    control, value = command["control"], command["value"]
    if not isinstance(control, str) or control not in DRIVERS[driver]["controls"]:
        raise IoTError("Tuto hodnotu nelze nastavovat.")
    if control in ("power", "locked"):
        check_bool(value)
    elif control == "target_temp_c":
        check_int(value, 0, 37)
    elif control == "timer_minutes":
        check_int(value, 0, 1440, 60)
    return control, value


class LocalTuya:
    def __init__(self, factory=None, sleeper=time.sleep):
        self.factory = factory
        self.sleeper = sleeper

    def _connect(self, module):
        factory = self.factory
        if factory is None:
            try:
                import tinytuya
                factory = tinytuya.Device
            except ImportError:
                raise IoTError("V API chybí TinyTuya. Přestavte IoT kontejner.", 503,
                               "dependency_missing") from None
        try:
            device = factory(module["device_id"], module["ip"], module["local_key"],
                             version=float(module["version"]))
            device.set_socketPersistent(False)
            device.set_socketTimeout(3)
            device.set_socketRetryLimit(2)
            device.set_socketRetryDelay(1)
            return device
        except Exception:
            raise IoTError("Nelze připravit spojení s modulem.", 502, "connection_failed") from None

    @staticmethod
    def _call(fn, *args):
        try:
            response = fn(*args)
        except Exception:
            raise IoTError("Modul neodpovídá v místní síti.", 502, "device_unavailable") from None
        if isinstance(response, dict) and ("Error" in response or "Err" in response):
            code = str(response.get("Err", ""))
            suffix = f" (TinyTuya {code})" if code.isdigit() else ""
            raise IoTError("Modul nepotvrdil komunikaci" + suffix + ".", 502,
                           "device_unavailable")
        return response

    @staticmethod
    def _state(module, response):
        if not isinstance(response, dict):
            raise IoTError("Modul nevrátil stav.", 502, "invalid_response")
        dps = response.get("dps")
        if not isinstance(dps, dict):
            data = response.get("data")
            dps = data.get("dps") if isinstance(data, dict) else None
        if not isinstance(dps, dict):
            raise IoTError("V odpovědi chybí stavové hodnoty.", 502, "invalid_response")
        dps = {str(k): v for k, v in dps.items()}
        try:
            power_dp = str(module["power_dp"]) if module["driver"] == "tuya_switch" else "1"
            state = {"online": True, "power": check_bool(dps[power_dp]),
                     "observed_at": utc_now(), "fault_code": 0, "faults": []}
            if module["driver"] == "bot_iph2":
                state.update({
                    "locked": check_bool(dps["2"]),
                    "target_temp_c": check_int(dps["3"], 0, 37),
                    "current_temp_c": check_int(dps["4"], 0, 99),
                    # Countdown může číst minuty mimo násobky 60.
                    "timer_minutes": check_int(dps["5"], 0, 1440),
                    "fault_code": check_int(dps["6"], 0, 2**32 - 1),
                })
                if state["fault_code"] & 1:
                    state["faults"].append("E1: porucha teplotního čidla")
                if state["fault_code"] & ~1:
                    state["faults"].append("Neznámý příznak poruchy")
            return state
        except (KeyError, IoTError):
            raise IoTError("Modul vrátil neúplný stav nebo neodpovídá zvolenému typu.",
                           502, "invalid_response") from None

    def status(self, module):
        device = self._connect(module)
        return self._state(module, self._call(device.status))

    def command(self, module, command):
        control, value = validate_command(module["driver"], command)
        dp = ({"power": 1, "locked": 2, "target_temp_c": 3, "timer_minutes": 5}[control]
              if module["driver"] == "bot_iph2" else module["power_dp"])
        device = self._connect(module)
        self._call(device.set_value, dp, value)
        self.sleeper(1)
        state = self._state(module, self._call(device.status))
        confirmed = state[control] == value
        if control == "timer_minutes" and value > 0:
            confirmed = 0 <= value - state[control] <= 1
        if not confirmed:
            raise IoTError("Příkaz byl odeslán, ale stav nepotvrdil požadovanou změnu.",
                           502, "write_unconfirmed")
        return state
