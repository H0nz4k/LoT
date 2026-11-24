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
| Adresa | ověřená LAN adresa panelu, TCP 6668 |
| Device ID | lokálně uložené ID panelu (ve veřejné dokumentaci vynecháno) |
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
read -r -p 'Device ID panelu: ' PANEL_ID
read -r -p 'LAN IP panelu: ' PANEL_IP
export PANEL_ID PANEL_IP
read -r -s -p 'Aktuální lokální klíč panelu: ' PANEL_LOCAL_KEY
printf '\n'
export PANEL_LOCAL_KEY

python3 - <<'PY'
import os
import tinytuya

panel = tinytuya.Device(
    os.environ["PANEL_ID"],
    os.environ["PANEL_IP"],
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
- Samostatný test TCP spojení na ověřenou LAN adresu panelu a TCP port 6668 prošel.
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
- Tapo P110M na vlastní ověřené LAN adrese má v repozitáři lokální ovladač
  `python-kasa`. HTTP výpis HUBu hlásil online stav a měření;
  samostatný přímý test Tapo nebyl touto terminálovou ukázkou doložen.
  Jeho protokol a přihlašování nejsou stejné jako u Tuya panelu.
- Sonoff ve stejném výpisu používal `sonoff_ewelink`, `source: ewelink`,
  `cloud: true`. Tento výpis nedokládá lokální ovládání Sonoffu.

Další poznatky o LAN komunikaci evidovat v repozitáři
[H0nz4k/LoT](https://github.com/H0nz4k/LoT), bez tajných údajů.

## Verze 1.2.1 — trvalý lokální klíč a ochrana aktualizací

Dashboard již používá cestu prohlížeč → lokální HTTP brána HUBu →
`LocalTuya` → panel TCP 6668. Lokální HTTP brána je nutná pro webový
prohlížeč; nejde o cloudové API výrobce. Klíč se neposílá do prohlížeče.
Zachovávají se ověření oprávnění, blokace offline/zámku/poruchy a readback.

Konfigurace `HANZHUB_TUYA_KEY_DIR` zapíná soubory `<device_id>.key`.
Při každém novém spojení ovladač soubor znovu přečte. Při nastaveném
adresáři nesmí chybějící či neplatný soubor způsobit návrat ke starému
klíči z databáze: daný panel hlásí `local_key_unavailable`. Ostatní
ovladače Tapo a Sonoff se nemění. Bez nastavené proměnné je zachováno
původní chování pro starší instalace.

Na HUB2 je klíč v `/var/lib/hanzhub/iot/keys/<DEVICE_ID>.key`.
Adresář má režim 700 a soubor 600, vlastník `hanz`. Klíč není součást
aplikace, aktualizačního archivu, manifestu ani Gitu. Nástroj jej uloží
atomicky; neplatné zadání nepřepíše předchozí soubor. Neobnovuje se
automaticky ze starého `devices.json` ani z dřívější databáze.

Po instalaci operátor zadá známý funkční klíč jednou v SSH terminálu.
`PANEL_ID` musí obsahovat skutečné lokální Device ID:

```bash
python3 /opt/hanzhub/iot-local-lan/tools/set-local-key.py \
  --directory /var/lib/hanzhub/iot/keys \
  --device-id "$PANEL_ID"
```

Nástroj se na klíč zeptá dvakrát bez zobrazení. Nezadávat klíč do chatu.
Restart služby není potřeba; běžná cache stavu může trvat osm sekund.
Ověření přes lokální službu je stále LAN komunikace.
`PANEL_MODULE_ID` je interní ID panelu z registru HUBu, nikoli Tuya Device ID:

```bash
curl -sS --max-time 20 \
  "http://127.0.0.1:4011/api/iot/devices/${PANEL_MODULE_ID}/state?fresh=1"
```

### Aktualizace na současném HUB2

Nepoužívat celý starý Docker release přes systemd runtime. Instalátor
`tools/install-hub2-panel-lan.py` aplikuje pouze podporu klíče na skutečný
živý ovladač, zachová ostatní místní změny, databázi, zařízení, eWeLink,
Tapo, Dashboard i InfoLCD. Vyžaduje plný SHA zdrojového commitu a čerstvý
SHA-256 živého `iot_driver.py`; při neshodě skončí před změnou runtime.
Před nasazením ověřit čistý checkout konkrétní revize a projektová pravidla.

```bash
git status --short
git rev-parse HEAD
sha256sum /opt/hanzhub-iot/iot/iot_driver.py
# Doplň přesné hodnoty z kontrol výše:
sudo python3 tools/install-hub2-panel-lan.py \
  --source-sha ZDROJOVY_COMMIT_40_HEX \
  --expect-driver-sha ZIVY_SHA256_64_HEX
```

Instalátor nejprve připraví zálohu a `restore.sh`, potom zapíše verzi,
zdrojový SHA, hashe runtime a konfigurace do
`/opt/hanzhub/iot-local-lan/manifest.json`. Restartuje pouze
`hanzhub-iot`; při selhání instalace obnoví předchozí soubory a službu.
Rollback zachová samostatně uložený klíč a databázi.

Systemd drop-in `47-local-key-file.conf` nastavuje adresář klíčů a
`ExecStartPre` kontrolu integrity. Při nečekaném přepsání ovladače,
modulu, verze nebo drop-inu kontrola při startu chybu zviditelní.
Neřeší úmyslné odstranění samotné kontroly ani všechny možné budoucí
instalátory; při aktualizacích ji zachovat a manifest obnovovat pouze
verzovaným LoT instalátorem. Aktualizace samotného Dashboardu neukládá
klíče a není vlastníkem tohoto runtime. Starý `install.py` při existenci
systemd IoT jednotky odmítne spustit paralelní Docker instalaci.

V Docker variantě lze klíče uložit do existujícího trvalého `iot_data`
volume a v `.env` nastavit `HANZHUB_TUYA_KEY_DIR=/var/lib/hanzhub-iot/keys`.
Příslušné soubory je třeba nejprve bezpečně vytvořit v tomto volume;
HUB2 instalátor není určený pro Docker.

[Ověřené mapování DP a test zápisu teploty](bot-iph2-dps.md) je evidováno
samostatně. Tato verze nepřidává neověřené povely ani automatické vypínání.

### Výsledek ověření verze 1.2.1

- Zdroj runtime: commit `ac2137318e25e0a27790a430a3fa109f73aac0e2`.
- 107 testů prošlo v odděleném Linux prostředí se syntetickými daty;
  [GitHub CI](https://github.com/H0nz4k/LoT/actions/runs/45310261582) prošla také.
- Po výslovném schválení operátorem byla souborová LAN podpora nasazena.
  Health a lokální čtecí brána Dashboardu odpovídají, kontrola integrity
  při startu prošla. Ostatní sledované služby zůstaly aktivní.
- Při první kontrole ještě nebyl zadaný klíč, proto panel správně hlásil
  `local_key_unavailable` namísto použití starého klíče z databáze.
  Po soukromém zadání klíče operátorem následné čtení IoT i brány
  běžícího Dashboardu vrátilo `online: true` a shodné skutečné teploty,
  zapnutí, zámek i časovač. Soubor má oprávnění 600 a vlastnictví účtu
  služby. Tapo také zůstalo online a vracelo skutečný příkon i spotřebu.
- Žádný nový fyzický povel ani změna teploty nebyly při nasazení provedeny.
  Energetický release nebyl tímto cíleným overlayem nasazován.
