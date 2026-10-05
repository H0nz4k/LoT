"""Nezávislé čtení IoT API a vykreslení panelu na stávající framebuffer LCD."""
import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

GREEN = (50, 220, 130)
RED = (248, 82, 82)
AMBER = (245, 158, 11)
WHITE = (255, 255, 255)


class PanelPoller:
    def __init__(self, url="http://127.0.0.1:4011/api/iot/panel", interval=5, module_id=None):
        self.url = url + (("&" if "?" in url else "?") + urlencode({"id": module_id}) if module_id else "")
        self.interval = max(2, interval)
        self.lock = threading.Lock()
        self.value = None
        self.received = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                with urlopen(self.url, timeout=4) as response:
                    payload = json.load(response)
                state = payload.get("state") if isinstance(payload, dict) else None
                state = validate_state(state)
            except (OSError, ValueError, TypeError):
                state = None
            with self.lock:
                self.value, self.received = state, time.monotonic()
            self.stopped.wait(self.interval)

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.received > max(15, self.interval * 3):
                return None
            return dict(self.value) if self.value else None

    def close(self):
        self.stopped.set()


def validate_state(state):
    if not isinstance(state, dict) or state.get("online") is not True or type(state.get("power")) is not bool:
        return None
    fault = state.get("fault_code", 0)
    if type(fault) is not int or not 0 <= fault < 2**32:
        return None
    if state["power"]:
        if type(state.get("current_temp_c")) is not int or not 0 <= state["current_temp_c"] <= 99:
            return None
        if type(state.get("target_temp_c")) is not int or not 0 <= state["target_temp_c"] <= 37:
            return None
    return state


def draw_panel_section(draw, pad, y, width, font_title, font, state):
    """Stejná výška jako původní LoRa sekce: nadpis a dva řádky."""
    state = validate_state(state)
    draw.text((pad, y), "Infrapanel", font=font_title, fill=WHITE)
    y += font_title.size + 2
    draw.line((pad, y, width - pad, y), fill=(80, 80, 80), width=2)
    y += 8
    x_value = pad + 120
    draw.text((pad, y), "Stav:", font=font, fill=WHITE)
    if state is None:
        label, color = "NEDOSTUPNY", AMBER
    elif state["power"]:
        label, color = "ON", GREEN
    else:
        label, color = "OFF", RED
    draw.text((x_value, y), label, font=font, fill=color)
    if state and state.get("fault_code"):
        draw.text((x_value + 55, y), "E1" if state["fault_code"] & 1 else "CHYBA", font=font, fill=RED)
    y += font.size + 2
    draw.text((pad, y), "Akt. / cil:", font=font, fill=WHITE)
    value = f'{state["current_temp_c"]} °C / {state["target_temp_c"]} °C' if state and state["power"] else "—"
    draw.text((x_value, y), value, font=font, fill=WHITE)
    return y + font.size + 2
