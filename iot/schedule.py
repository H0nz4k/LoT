"""Týdenní plán infrapanelu. Jediný plánovač IoT služby, čas Europe/Prague."""
from datetime import datetime
from zoneinfo import ZoneInfo

from iot_driver import IoTError, check_int

DEFAULT_PRESETS = (17, 19, 21)
DAY_LABELS = ("Po", "Út", "St", "Čt", "Pá", "So", "Ne")


def prague_now(moment=None):
    try:
        zone = ZoneInfo("Europe/Prague")
    except Exception:
        raise IoTError("Chybí časová zóna Europe/Prague.", 503, "timezone_unavailable") from None
    if moment is None:
        return datetime.now(zone)
    if not isinstance(moment, datetime):
        raise IoTError("Neplatný čas plánovače.")
    if moment.tzinfo is None:
        raise IoTError("Čas plánovače musí mít zónu.")
    return moment.astimezone(zone)


def parse_hhmm(value):
    if isinstance(value, str) and len(value) == 8 and value.endswith(":00"):
        value = value[:5]
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise IoTError("Čas zadejte ve tvaru HH:MM.")
    hour, minute = value[:2], value[3:]
    if not (hour.isdigit() and minute.isdigit()):
        raise IoTError("Čas zadejte ve tvaru HH:MM.")
    hours, minutes = int(hour), int(minute)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise IoTError("Čas zadejte ve tvaru HH:MM.")
    return hours * 60 + minutes


def format_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_days(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 7:
        raise IoTError("Vyberte alespoň jeden den v týdnu.")
    days = []
    for day in value:
        if type(day) is not int or not 0 <= day <= 6:
            raise IoTError("Den musí být číslo 0 (pondělí) až 6 (neděle).")
        if day not in days:
            days.append(day)
    return days


def validate_rule(data):
    if not isinstance(data, dict) or set(data) - {"days", "start", "end", "target_temp_c", "enabled"}:
        raise IoTError("Neplatná pole pravidla.")
    for field in ("days", "start", "end", "target_temp_c"):
        if field not in data:
            raise IoTError("Pravidlo musí mít dny, začátek, konec a cílovou teplotu.")
    days = parse_days(data["days"])
    start = parse_hhmm(data["start"])
    end = parse_hhmm(data["end"])
    if end <= start:
        raise IoTError("Konec vytápění musí být ve stejný den po začátku.")
    enabled = data.get("enabled", True)
    if type(enabled) is not bool:
        raise IoTError("Položka enabled musí být true nebo false.")
    return {
        "days": days,
        "start_min": start,
        "end_min": end,
        "target_temp_c": check_int(data["target_temp_c"], 0, 37),
        "enabled": enabled,
    }


def rule_public(rule):
    return {
        "id": rule["id"],
        "days": list(rule["days"]),
        "start": format_hhmm(rule["start_min"]),
        "end": format_hhmm(rule["end_min"]),
        "target_temp_c": rule["target_temp_c"],
        "enabled": bool(rule["enabled"]),
    }


def active_rule(rules, moment):
    """Platí interval [začátek, konec). Při překryvu vyhraje později začínající pravidlo."""
    moment = prague_now(moment)
    minute = moment.hour * 60 + moment.minute
    day = moment.weekday()
    matching = [
        rule for rule in rules
        if rule.get("enabled") and day in rule["days"] and rule["start_min"] <= minute < rule["end_min"]
    ]
    if not matching:
        return None
    return max(matching, key=lambda rule: (rule["start_min"], rule["target_temp_c"], rule["id"]))


def validate_presets(data):
    if not isinstance(data, dict) or set(data) != {"temps"}:
        raise IoTError("Rychlé volby musí obsahovat právě položku temps.")
    temps = data["temps"]
    if not isinstance(temps, list) or len(temps) != 3:
        raise IoTError("Zadejte právě tři teploty ve °C.")
    return [check_int(value, 0, 37) for value in temps]
