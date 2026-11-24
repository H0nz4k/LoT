# Přímá komunikace se zařízeními v LAN

## Dohoda a ověřený stav — 24. 11. 2026

Pro infrapanel používáme přímou komunikaci v místní síti, bez cloudového API
výrobce a bez prostředníka HTTP API HanzHubu. TinyTuya je klientská knihovna
pro šifrovaný Tuya protokol přímo mezi HUBem a panelem; není to cloudové API.

Tento záznam vychází z výpisů a screenshotů uživatelského terminálu na HUB2.
Autor dokumentace nové spojení se zařízením ani zápis neprováděl.

| Parametr | Ověřená hodnota |
| --- | --- |
| Zařízení | Infrapanel BOT IPH2 |
| Adresa | `192.168.1.51:6668`, TCP |
| Device ID | `bf66247c13125cd7bcs0vj` |
| Protokol | Tuya 3.4 |
| Klient | TinyTuya 1.21.1, Python 3, HUB2 |
| Python prostředí | `~/panel-test-venv` |
| Autentizace | Aktuální lokální klíč, který se nezapisuje do Gitu |
| Počet pokusů | `set_socketRetryLimit(1)` |

IP je údaj z tohoto ověření, nikoli záruka neměnné DHCP adresy.
Port 6668 není HTTP server: samotný `curl`, otevření portu nebo prostý text
neodešlou platný Tuya povel. Otevřené TCP spojení samo neověřuje klíč.

Úspěšná odpověď na přímé `panel.status()`:

```python
{'dps': {'1': False, '2': False, '3': 25, '4': 19, '5': 0, '6': 0}}
```

| DP | Význam | Pozorovaná hodnota |
| --- | --- | --- |
| 1 | Zapnutí | Vypnuto |
| 2 | Dětský zámek | Vypnutý |
| 3 | Cílová teplota | 25 °C |
| 4 | Aktuální teplota | 19 °C |
| 5 | Časovač | 0 minut |
| 6 | Poruchový bitový kód | 0 |

Jde o historický odečet, nikoli aktuální živé hodnoty.

## Reprodukce čtení přímo z HUBu

Příkazy jsou pro Bash po SSH přihlášení na HUB.
Prostředí `~/panel-test-venv` již při ověření obsahovalo TinyTuya;
znovu jej nevytvářet ani kvůli testu neinstalovat do systémového Pythonu.

Vlož celý blok. Lokální klíč se zadává skrytě a skript pouze čte stav:

```bash
source ~/panel-test-venv/bin/activate
read -r -s -p 'Aktuální lokální klíč panelu: ' PANEL_LOCAL_KEY
printf '\n'
export PANEL_LOCAL_KEY

python3 - <<'PY'
import os
import tinytuya

panel = tinytuya.Device(
    "bf66247c13125cd7bcs0vj",
    "192.168.1.51",
    os.environ["PANEL_LOCAL_KEY"],
    version=3.4,
)
panel.set_socketTimeout(5)
panel.set_socketRetryLimit(1)
print(panel.status())
PY

unset PANEL_LOCAL_KEY
```

Python kód musí běžet přes `python3`, nikoli přímo v Bash promptu.
Klíč nevkládat do příkazové řádky, dokumentace, screenshotů ani historie Gitu.
Před každým novým spuštěním bloku jej znovu zadat; závěrečné `unset` jej odstraní
z prostředí shellu.

## Odeslání povelu a kontrola výsledku

V existujícím ovladači je změna cílové teploty mapována na
`panel.set_value(3, požadovaná_teplota)`, zapnutí na DP 1,
zámek na DP 2 a časovač na DP 5. Mapování platí pro tento BOT IPH2,
nikoli automaticky pro jiné Tuya zařízení.

Po výslovně zvoleném povelu je nutné znovu načíst `panel.status()`
a porovnat příslušné DP s požadavkem. Samotná odpověď na zápis ani
otevřený TCP port nejsou potvrzením změny. Respektovat zámek,
poruchu a nejistý výsledek; neověřený zápis automaticky neopakovat.

Pokus nastavit 22 °C se v této relaci nepotvrdil: původní skript vracel
chybu 905. Po opravě počtu pokusů následovalo pouze úspěšné čtení.
**Přímé čtení je ověřené, úspěšný přímý zápis touto relací doložen není.**

## Co se při diagnostice opravilo

- Systémový `python3` neměl TinyTuya; samostatné prostředí jej obsahuje.
- Původní ukázka měla chybný `set_socketRetryLimit(0)` a vracela 905
  (`Device Unreachable`). Nulu nepoužívat; může potlačit pokus o spojení.
- Samostatný test TCP spojení na `192.168.1.51:6668` prošel.
- Se stejnou adresou, protokolem 3.4, zadaným lokálním klíčem a
  `set_socketRetryLimit(1)` pak panel vrátil výše uvedené DPS.
- Předchozí HTTP výpis HanzHubu hlásil pro panel TinyTuya 914.
  Úspěšný samostatný test neprokazuje opravu běžící IoT služby ani shodu
  jejího uloženého klíče s klíčem zadaným v terminálu. Chyba 914 sama
  nedokazuje rotaci klíče; může znamenat problém při navázání relace.

Kvůli této diagnostice neimportovat starý `devices.json`, neměnit
produkční klíč a neprovádět obnovu napájení.

## Rozlišení dalších komunikačních cest

- `http://127.0.0.1:4011/api/iot/devices` je HTTP API HanzHub IoT.
  Jeho volání přes `curl` není přímé spojení s panelem.
- Tapo P110M na `192.168.1.52` má v repozitáři lokální ovladač
  `python-kasa`. HTTP výpis HUBu hlásil online stav a měření;
  samostatný přímý test Tapo nebyl touto terminálovou ukázkou doložen.
  Jeho protokol a přihlašování nejsou stejné jako u Tuya panelu.
- Sonoff ve stejném výpisu používal `sonoff_ewelink`, `source: ewelink`,
  `cloud: true`. Tento výpis nedokládá lokální ovládání Sonoffu.

Další poznatky o LAN komunikaci evidovat v repozitáři
[H0nz4k/LoT](https://github.com/H0nz4k/LoT), bez tajných údajů.
