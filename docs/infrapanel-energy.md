# Vytápění – infrapanel: implementace a předání

Stav 20. 11. 2026: implementováno ve větvi `codex/infra-energy-monitoring`, **nenasazeno**.
Výchozí větev `codex/infra-panel-fresh-state`, commit `94e5ce5`, zachovává aktuální opravu vynuceného čtení Sonoff stavu.
Obsahové SHA-256 sedmi relevantních souborů této větve souhlasí s běžícím kontejnerem po normalizaci CRLF/LF.
Produkční checkout má vlastní necommitované změny; nepoužívat na něm checkout/reset/pull ani jej nahradit čistým mainem.

## Ověřená identita a hranice dat

- `1002a4a7e6`: Sonoff S60TPF, UIID 190, lokální modul `b2f0eabc93bb`, povolený a online při čtecím průzkumu. Aplikační název již **Zás. infra**; cloudový název je **Zásuvka 2**. Cloudové názvy nejsou aplikační názvy a klient je nepřepisuje při čtení.
- Jiný modul se jménem **Zás. infra** v aktuálním registru není. Druhá registrovaná zásuvka Tapo má jméno **Zasuvka**. Není doloženo, že je požadovanou původní „Zás. infra“; její název ani ID neměníme. Automatizace i identity zůstávají stejné.
- API skutečně vrací `dayKwh`, `monthKwh`, `power`, `voltage`. Při průzkumu byly okamžité elektrické údaje včetně napětí nulové; takový příkon se nepovažuje za dostupné měření.
- S60TPF podporuje energetický monitoring: [manuál výrobce](https://support.sonoff.tech/s60tpf-usermanual/). Převody pro UIID 190 jsou ověřeny proti [primární implementaci SonoffLAN](https://github.com/AlexxIT/SonoffLAN/blob/master/custom_components/sonoff/core/devices.py): W, V i kWh jsou setiny surové hodnoty. Nejde o univerzální převod ostatních Sonoff modelů.
- Read-only REST snapshot neposkytl čas jednotlivého měření ani pole `hoursKwhData`. Přítomnost požadavku `getHoursKwh` v parametrech není historická odpověď. Import historie není aktivován, historii nevymýšlíme. Aktivní vyžádání historie přes řídicí endpoint zde nepřidáváme.
- Cloudová metadata nepotvrzují fyzickou polohu relé ani stáří elektrického údaje. GUI to výslovně uvádí.

## Ukládání a výpočty

`iot/energy.py` používá stávající `IoTManager._db()` a `modules.sqlite3`. Přidává jen tabulky `energy_samples`, `energy_intervals`, `energy_tariffs`, `energy_events` a indexy. Existující tabulky zařízení, plánů a událostí nemaže ani nemění. Nový proces ani Compose služba nevznikají.

Při startu `iot/server.py` začne samostatné vlákno sbírat přibližně po 60 sekundách. Stávající `restart: unless-stopped` a persistentní `iot_data` zajišťují opětovný start. Vynucené čtení používá stávající eWeLink klient se společným omezením rychlosti. Sběr neposílá ovládací příkazy. Při obsazeném zámku zařízení nebo aktivní/nevyřešené obnově sběr vynechá; nečeká na řízenou obnovu. Změna obnovy během čtení zneplatní vzorek. Síťové čtení nedrží zámek zařízení ani SQLite transakci. Již rozběhnutý cloudový dotaz může zpozdit úvodní ověření obnovy až o svůj timeout; během vypnutí monitoring nové dotazy neposílá. Obnova nespoléhá na dostupnost sběrného vlákna a selhání inicializace energetické evidence neblokuje samostatné obnovení dřívějšího ON záměru.

- UTC epochy v databázi, agregace podle `Europe/Prague`, skutečná délka dne při DST.
- Od 20. 11. 2026, první dostupný vzorek slouží pouze jako základ. Starší hodnoty dne/měsíce se do evidence nepřičtou.
- Unikátní minutový slot brání duplicitám; transakce atomicky ukládá vzorek i jeho intervaly. Stav přežije restart.
- Měsíční počítadlo je primární uvnitř téhož měsíce, denní je záloha uvnitř téhož dne. Pouze při potvrzené konfiguraci `TZ=Europe/Prague`. Neznámá hranice měsíce s chybějícím odečtem se nedoplňuje.
- Záporný nebo fyzikálně nepravděpodobný rozdíl se zaznamená jako reset/neplatný odečet. Nevznikne záporná energie ani dopočtený resetový interval.
- Opakovaný stejný cloudový čítač bez časové značky zařízení nevytváří naměřenou nulu. Následující přírůstek se počítá od prvního stejného snapshotu. Výpadek může být energeticky překlenut počítadlem; rozdělení v čase se označí jako odhad a chybějící měření zůstanou evidována.
- Bez srovnatelných počítadel lze orientačně integrovat dostupný příkon lichoběžníkovým pravidlem nejvýše přes 180 s. Metoda je `estimated`; mezery se neintegrují.
- Provoz je dílčí odhad času, kdy oba sousední dostupné příkony překračují 5 W. Zapnuté relé se nepovažuje za dobu skutečného topení.
- Výchozí tarif je 2,512 Kč/kWh. Nový tarif platí od času uložení; interval se rozdělí na hranicích tarifu a pražského dne. Staré náklady se nepřecení. Rozdělení energie mezi tarify/dny je odhad, protože přesný okamžik přírůstku čítače chybí. Peněžní výpočet používá Decimal a zobrazení/CSV dvě desetinná místa.
- Chybějící spotřeba, cena i provoz jsou `null`, nikoli nula. Tabulka a CSV uvádějí neúplnost i původ.

API: `GET /api/iot/heating?from=YYYY-MM-DD&to=YYYY-MM-DD`, stejný filtr `/api/iot/heating.csv`, `POST /api/iot/heating/tariff` s `{"czk_per_kwh":"2.512"}`. Čtení zachovává Cloudflare Access; tarif lze měnit pouze z LAN. Nejvýše deset let na jeden přehled. Souhrny agreguje SQLite, graf příkonu má omezený počet hodinových/denních bodů a explicitní mezery.

## Obnovení připojení: dohledání a bezpečná implementace

V běžícím kontejneru, produkčním checkoutu, aktuálních vzdálených větvích ani LoT PR #1–#6 nebyla nalezena uvedená 30sekundová obnova napájení. Po doplnění vlastníka byly prověřeny také popisy a metadata všech 112 dostupných otevřených/uzavřených PR `H0nz4k/HanzHub-Control`, seznam větví a historie všech načtených vzdálených referencí včetně vyhledání ID, Sonoff/eWeLink a power-cycle implementací. Nalezené eWeLink změny chrání produkční mounty; [PR #97](https://github.com/H0nz4k/HanzHub-Control/pull/97) je jiný jednosekundový GPIO19 pulz a do tohoto projektu se nepřenáší. Výchozí LoT `94e5ce5` řeší čerstvé čtení stavu, nikoli power-cycle. Požadovaná implementace nebyla v dostupných zdrojích nalezena.

Na výslovný pokyn vlastníka je proto doplněn `iot/recovery.py` přímo v IoT, přes původní Sonoff adaptér. Nová tabulka `infra_recovery` a existující `energy_events` uchovávají trvalý záměr a fáze. Žádný GPIO, InfoLCD, broker ani Henk workflow se nemění.

- Spuštění je pouze z LAN s existující Origin/autentizační ochranou, explicitním potvrzením `power-cycle-30s`, správným Device ID a UUID v4. HTTP 202 znamená přijetí, nikoli dokončení. GET stav je dostupný i přes ověřené Cloudflare Access. GUI má potvrzovací dialog; Zrušit/Escape neposílá příkaz.
- Před vypnutím se čerstvě ověří místní modul i cloudové ID, SONOFF/Coolkit, model S60TPF, UIID 190, jediný outlet 0, vypnutý pulzní režim, dostupnost a původně **zapnutý** stav. Nejednoznačná identita, offline nebo původně vypnutá zásuvka cyklus odmítnou bez příkazu.
- Záměr OFF se musí atomicky uložit před jediným vypnutím. Duplicitní UUID vrátí tentýž záznam bez dalšího OFF. Databázový unikátní aktivní záznam, procesový kernelový zámek a stávající zámek modulu chrání souběh i druhý proces.
- Po cloudovém potvrzení OFF následuje `time.sleep(30)`; běžné čekání není závislé na změně nástěnných hodin. Poté se znovu ověří identita, odešle ON a čerstvě ověří zapnutí. Fyzické relé cloudové potvrzení samo o sobě nedokládá.
- Jakákoli chyba po OFF záměru přejde do bezpečného návratu ON, bez opakování OFF a bez dalšího 30s čekání. Nejvýše tři pokusy ON s dvousekundovým odstupem; chyba logování je nesmí přeskočit. Pokud už je ON potvrzený, další zápis není potřeba. Neověřené zapnutí vede do `needs_review`: nový cyklus OFF je blokován, GUI nabízí pouze explicitní obnovení ON.
- Po restartu se nedokončený durable OFF záměr obnovuje **pouze ON**. Předchozí souhlas platil i pro návrat napájení; restart nevytvoří další OFF. Přerušení před OFF záměrem končí bez ovládání. Při ztrátě identity/API se neovládá jiné zařízení a stav zůstává nevyřešený. Úplná ztráta databáze/sítě nemůže zaručit fyzické zapnutí a vyžaduje místní kontrolu.
- Monitoring v průběhu obnovy ukládá mezeru. Integrace příkonu ani odhad provozu nepřeklenují vypínací interval; známý přírůstek elektroměru zůstává použitelný jako naměřená energie s odhadem časového rozdělení. Události zahrnují zahájení, záměr, vypnutí, zapnutí, výsledek i chybu. Energetická historie se nemaže.

Rozhraní obnovy: `GET /api/iot/heating/recovery`, `POST` na stejné cestě s `request_id`, `device_id` a `confirm`; ON-only opakování používá `POST /api/iot/heating/recovery/restore` s `operation_id` a `confirm=restore-on`. Načtení stránky, sběr dat ani spuštění služby nevytvářejí nový vypínací cyklus.

**Implementováno a simulací ověřeno, nenasazeno. Žádný fyzický ON/OFF nebyl při této práci proveden.** Chybějící původní implementace již vývoj neblokuje; produkční nasazení a fyzické ověření nadále vyžadují samostatné schválení.

## Ověření a reprodukce

```bash
python -m unittest discover -s tests -v
python -m compileall -q iot lcd integrations install.py
node --check web/heating.js
node --check web/iot.js
PLAYWRIGHT_MODULE=playwright PYTHON=python node tests/browser-energy.cjs
```

`tests/energy_preview.py` je pouze izolovaná syntetická fixture: dočasná DB, loopback, žádný scheduler, collector ani skutečná zařízení; Sonoff je paměťová náhrada a čekání je v testu nahrazené. Prohlížeč ověřuje desktop 1440 px a mobil 390 px, grafy, období, řazení, CSV, tarif, potvrzení/zrušení obnovy včetně Escape po předchozím potvrzení a návrat k ovládání. Neprokazuje fyzickou dobu vypnutí ani skutečné ukládání produkčních údajů.

Windows: 13 energetických a 15 obnovovacích cílených testů prošlo. Celá původní sada měla tři selhání POSIX cest/oprávnění a jednu chybu úklidu otevřeného SQLite spojení v původním testu; tyto nesouvisející testy nebyly přepisovány. Konečná linuxová sada byla spuštěna v jednorázovém kontejneru z existujícího runtime image s `--network none --read-only`, omezenými prostředky a výhradně zdrojovým mountem, bez produkčních dat; **94 testů prošlo**. Pokryty jsou chybná identita, offline, duplicitní a souběžné požadavky, mezery monitoringu, 30s čekání se simulovaným časem, nejisté OFF, omezené ON retry, chyba DB před/po OFF, restart a ON-only návrat, HTTP potvrzení a cizí Origin. Prošel prohlížečový průchod na obou šířkách, Python compileall a JS syntaxe. Impeccable hlásí pouze zděděné vizuální nedostatky mimo novou sekci; nové popisky mají 14 px a nová sekce na mobilu nepřetéká přes celou stránku.

## Návrh nasazení ke schválení (neprovádět nyní)

1. Nechat zrevidovat přesný commit v PR #7 a schválit nasazení pouze IoT služby. Znovu přečíst image ID, otisky dotčených souborů a registr zařízení; při odchylce od základu znovu porovnat změny. Žádné přepsání živého zdroje.
2. Vytvořit chráněnou zálohu necommitovaných zdrojů, `.env`, Compose a konzistentní SQLite zálohu přes `sqlite3.Connection.backup()`; zálohy ani přihlašovací údaje nepřesouvat do webu nebo Gitu. Zaznamenat mount existujícího `iot_data` a eWeLink dat beze změn.
3. V odděleném checkoutu stejného repozitáře sestavit image z přesného schváleného commitu. Neměnit GPIO, LCD, automatizace, sítě ani jiné služby. Uschovat starý image a Compose preimage; nepoužívat `down -v` ani prune.
4. Po explicitním schválení nahradit pouze image `hanzhub_iot` přes jeho zachovanou produkční Compose konfiguraci. Nepouštět obecný instalátor s LCD/dashboard integrací. Přidané tabulky jsou aditivní a ostatní registrace nemění.
5. Ověřit skutečné dva a více časově různých vzorků v DB, přežití restartu IoT služby, nepřítomnost duplicit, API/CSV, správná ID/názvy/plány a změnu počítadla při přirozeném provozu. Nulové napětí či nezměněná cache není důkaz skutečné nulové spotřeby. Nevyvolávat fyzický power-cycle bez dalšího souhlasu.
6. Návrat: nejprve ověřit, že neběží ani není nevyřešený obnovovací cyklus. Při pending OFF/ON stavu nevracet starý image, který journal nezná; nejprve samostatně vyřešit návrat ON. Potom znovu spustit pouze původní image s původní konfigurací a stejným datovým svazkem; nové tabulky mohou zůstat a původní verze je ignoruje. Obnovení celé DB ze zálohy by ztratilo novější změny, proto není automatickým rollbackem a vyžaduje samostatné rozhodnutí.

Ověřený původní image: `hanzhub-iot:infra-fresh-20261118`, ID `sha256:1ca28d629fb54e0add95bfc40bf2608d9f6f699e6a5a93d8fcd57d49c93211a3` (před skutečným nasazením znovu ověřit).
