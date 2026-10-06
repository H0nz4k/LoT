"""Lokální TP-Link Tapo P110M přes python-kasa. Přihlašovací údaje zůstávají na HUBu."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import math
import logging
import threading

from iot_driver import IoTError, check_bool, utc_now, validate_command

# Kasa diagnostiku neposíláme do veřejných kontejnerových logů; chyby mapujeme níže.
_logger = logging.getLogger("kasa")
_logger.addHandler(logging.NullHandler())
_logger.propagate = False


class LocalTapo:
    """Kasa objekty i HTTP spojení žijí v jediném asyncio vlákně."""
    def __init__(self, factory=None, timeout=16):
        self.factory = factory
        self.timeout = timeout
        self._configs = {}
        self._loop = None
        self._thread = None
        self._guard = threading.Lock()

    def _start(self):
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever,
                                                name="tapo-io", daemon=True)
                self._thread.start()

    def close(self):
        with self._guard:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=3)
                if not self._thread.is_alive():
                    self._loop.close()
                    self._loop = None
                    self._configs.clear()

    async def _connect(self, module):
        if self.factory is not None:
            return await self.factory(module)
        try:
            from kasa import Device, Discover
        except ImportError:
            raise IoTError("V API chybí python-kasa. Aktualizujte IoT kontejner.",
                           503, "dependency_missing") from None
        # Změna IP nebo účtu vynutí nové rozpoznání protokolu. Konfigurace je jen v paměti.
        key = (module["ip"], module["tapo_username"], module["tapo_password"])
        config = self._configs.get(key)
        if config is not None:
            return await Device.connect(config=config)
        device = await Discover.discover_single(module["ip"],
            username=module["tapo_username"], password=module["tapo_password"],
            discovery_timeout=2, timeout=3)
        if device is None:
            raise IoTError("Zásuvka neodpovídá v místní síti. Ověřte její IP a Wi-Fi.",
                           502, "device_unavailable")
        self._configs[key] = device.config
        # Omezí i cache po opakovaných změnách přihlašovacích údajů.
        if len(self._configs) > 100:
            self._configs.pop(next(iter(self._configs)))
        return device

    @staticmethod
    def _identity(device, module):
        model = str(device.model).split("(", 1)[0].strip().upper()
        if model != "P110M":
            raise IoTError("Na této IP nebyla nalezena zásuvka Tapo P110M.",
                           502, "wrong_device")
        mac = str(device.mac or "").lower().replace("-", ":")
        device_id = str(device.device_id or "")
        if not mac or not device_id:
            raise IoTError("Zásuvka nevrátila identifikaci zařízení.", 502, "invalid_response")
        if ((module.get("mac") and module["mac"] != mac) or
                (module.get("device_id") and module["device_id"] != device_id)):
            raise IoTError("Na této IP odpovídá jiná zásuvka. Zkontrolujte její adresu.",
                           502, "identity_mismatch")
        return {"model": model, "mac": mac, "device_id": device_id}

    @staticmethod
    def _measurement(energy, attribute):
        try:
            value = getattr(energy, attribute, None)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                return float(value)
        except Exception:
            pass  # Chybějící měření nesmí znemožnit zapnutí/vypnutí.
        return None

    def _state(self, device, module):
        state = {**self._identity(device, module), "online": True,
                 "power": check_bool(device.is_on), "observed_at": utc_now(),
                 "fault_code": 0, "faults": []}
        # Module.Energy je řetězcový identifikátor "Energy" v python-kasa.
        energy = device.modules.get("Energy")
        for field, attribute in (("power_w", "current_consumption"),
                                 ("energy_today_kwh", "consumption_today"),
                                 ("energy_month_kwh", "consumption_this_month"),
                                 ("voltage_v", "voltage"), ("current_a", "current")):
            state[field] = self._measurement(energy, attribute)
        overheated = device.features.get("overheated")
        if overheated is not None and overheated.value is True:
            state.update(fault_code=1, faults=["Zásuvka hlásí přehřátí."])
        return state

    async def _operation(self, module, command=None):
        device = None
        try:
            device = await self._connect(module)
            await device.update()
            self._identity(device, module)  # Ověření proběhne před jakýmkoli zápisem.
            if command is not None:
                _, value = validate_command(module["driver"], command)
                await (device.turn_on() if value else device.turn_off())
                await asyncio.sleep(.4)
                await device.update()
            state = self._state(device, module)
            if command is not None and state["power"] != command["value"]:
                raise IoTError("Příkaz byl odeslán, ale zásuvka nepotvrdila změnu stavu.",
                               502, "write_unconfirmed")
            return state
        finally:
            if device is not None:
                try:
                    await asyncio.wait_for(device.disconnect(), timeout=2)
                except Exception:
                    pass

    async def _bounded(self, module, command):
        return await asyncio.wait_for(self._operation(module, command), self.timeout)

    def _run(self, module, command=None):
        self._start()
        future = asyncio.run_coroutine_threadsafe(self._bounded(module, command), self._loop)
        try:
            return future.result(timeout=self.timeout + 1)
        except IoTError:
            raise
        except (TimeoutError, FutureTimeout):
            future.cancel()
            raise IoTError("Zásuvka včas neodpověděla. Ověřte její IP a Wi-Fi.",
                           502, "device_unavailable") from None
        except Exception as error:
            # Neserializovat raw Kasa výjimky: mohou obsahovat účet nebo heslo.
            try:
                from kasa import AuthenticationError
                authentication_failed = isinstance(error, AuthenticationError)
            except ImportError:
                authentication_failed = False
            if authentication_failed:
                raise IoTError("Přihlášení k Tapo se nezdařilo. Ověřte účet a heslo; v Tapo případně povolte kompatibilitu třetích stran.",
                               502, "authentication_failed") from None
            raise IoTError("Se zásuvkou nelze komunikovat. Ověřte Wi-Fi, IP a přihlášení Tapo.",
                           502, "device_unavailable") from None

    def status(self, module):
        return self._run(module)

    def command(self, module, command):
        validate_command(module["driver"], command)
        return self._run(module, command)
