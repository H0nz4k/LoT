# BOT IPH2 — mapování lokálních hodnot a příkazů

Ověřeno 24. 11. 2026 přímým lokálním Tuya spojením a následným zápisem
přes lokální bránu Dashboardu. Nepoužilo se cloudové API výrobce.
Konkrétní LAN adresy, ID zařízení a klíče do této veřejné mapy nepatří.

## Zpřístupněné datové body

`status()` → `detect_available_dps()` → `status()` v TinyTuya 1.20.0
nad protokolem 3.4 vrátilo stejnou sadu DP 1–6. Dotaz byl pouze čtecí.
Knihovna u výchozího typu zařízení vrací rozpoznanou sadu z odpovědi;
nejde o důkaz neexistence dalších skrytých nebo servisních DP ve firmwaru.
Neznámé DP se nezkoušejí náhodnými zápisy.

| DP | Typ | Mapování v ovladači | Čtení | Skutečný zápis v této relaci |
| --- | --- | --- | --- | --- |
| 1 | boolean | `power`, zapnutí panelu | Hodnota přečtena | Neprováděn |
| 2 | boolean | `locked`, dětský zámek | Hodnota přečtena | Neprováděn |
| 3 | integer | `target_temp_c`, cílová teplota ve °C | Potvrzeno | **22 → 23 → 22 °C**, oba kroky potvrzeny |
| 4 | integer | `current_temp_c`, aktuální teplota ve °C | Potvrzeno | Pouze pro čtení v aplikaci |
| 5 | integer | `timer_minutes`, časovač; aplikace používá minuty | Přečtena nula | Nenulový odpočet ani jednotka časovým testem neověřeny |
| 6 | integer | `fault_code`, poruchový bitový kód | Přečtena nula | Pouze pro čtení v aplikaci; poruchy nevyvolávány |

Význam DP vychází ze stávajícího ovladače a uvedených pozorování.
Samotný Tuya dotaz nevrací názvy, jednotky ani min/max schéma výrobce.

## Podporované příkazy aplikace

| Příkaz | DP | Hodnota povolená aplikací | Stav ověření |
| --- | --- | --- | --- |
| `power` | 1 | `true` / `false` | Implementováno; spínání v této relaci netestováno |
| `locked` | 2 | `true` / `false` | Implementováno; zamykání v této relaci netestováno |
| `target_temp_c` | 3 | Celé číslo 0–37 | Živě ověřeny hodnoty 22 a 23; krajní hodnoty netestovány |
| `timer_minutes` | 5 | 0–1440, násobky 60 | Implementováno; časový průběh netestován |

Rozsahy jsou **validace současné aplikace**, nikoli experimentálně zjištěné
limity zařízení. Rozsah cílové teploty ani chování na hranicích nelze
odvodit z jednoho úspěšného testu. Na DP 4 a 6 aplikace zápis nepovoluje.
Stávající dekódování bitu 0 DP 6 jako chyby čidla není touto relací
hardwarově ověřené; žádná závada se kvůli testu nevyvolávala.

## Doložený test teploty

Operátor výslovně schválil zvýšení právě aktuální cílové teploty o 1 °C
a návrat na původní hodnotu. Před povelem byla ověřena dostupnost,
vypnutý dětský zámek a absence hlášené poruchy.

| Krok | Čas UTC | Cíl | Aktuální teplota | Potvrzení |
| --- | --- | --- | --- | --- |
| Výchozí stav | 02:17:14 | 22 °C | 18 °C | Čerstvé čtení |
| Zvýšení | 02:17:15 | 23 °C | 18 °C | Zápis a readback panelu |
| Návrat | 02:17:16 | 22 °C | 18 °C | Zápis a readback panelu |

Zapnutí zůstalo `true`, zámek `false`, časovač 0 a poruchy prázdné.
Test neodesílal povely zapnutí, vypnutí, zámku ani časovače a neměřil
tepelnou odezvu nebo fyzické sepnutí relé. Výsledkem je potvrzení
protokolového zápisu a obnovy původního setpointu, nikoli test výkonu.

## Opakování pouze čtecího průzkumu

Nástroj `tools/probe-panel-dps.py` nepřidává trvalou službu ani žádné
ovládání. Používá existující soubor lokálního klíče; nebere klíč přes
argumenty a nevrací jeho obsah. Neznámé řetězcové hodnoty DP skrývá.
Spustit z konkrétní ověřené revize LoT v prostředí s TinyTuya:

```bash
export HANZHUB_TUYA_KEY_DIR=/cesta/k/soukromym/klicum
read -r -p 'Device ID panelu: ' PANEL_ID
read -r -p 'LAN IP panelu: ' PANEL_IP
python3 -B tools/probe-panel-dps.py \
  --device-id "$PANEL_ID" --ip "$PANEL_IP" --version 3.4
```

Další případné testy zapnutí, zámku nebo časovače musí mít předem určený
rozsah a návrat do původního stavu. Tento dokument není souhlasem
k jejich automatickému spuštění.
