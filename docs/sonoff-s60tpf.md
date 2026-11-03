# SONOFF S60TPF v HanzHubu

## Co je ověřené a jaká jsou omezení

S60TPF je původní Wi-Fi zásuvka eWeLink. Výrobce uvádí dálkové ovládání,
měření a denní/měsíční historii v aplikaci. S60TPF-M je jiné zařízení s Matter;
ovladač `sonoff_s60tpf` tuto variantu záměrně odmítá.

Implementace používá oficiální eWeLink OAuth v2 přes HTTPS. Ve veřejné
dokumentaci výrobce jsme nedoložili přímé lokální REST API pro S60TPF ani
podporu SONOFF DIY Mode. Podpora LAN v aplikaci sama neznamená veřejné API.
eWeLink CUBE má vlastní lokální Open API, ale vyžaduje samostatnou bránu a
ověření kompatibility zařízení; její endpointy nejsou endpointy zásuvky.
Tuto integraci tedy nelze ovládat při výpadku internetu nebo cloudu.

Oficiální dokumentace:

- [Manuál S60TPF](https://support.sonoff.tech/s60tpf-usermanual/)
- [Manuál odlišné S60TPF-M](https://support.sonoff.tech/s60tpf-m-usermanual/)
- [eWeLink OAuth v2, oprávnění a regiony](https://github.com/CoolKit-Technologies/eWeLink-API/blob/main/en/OAuth2.0.md)
- [Publikované parametry UIID](https://github.com/CoolKit-Technologies/eWeLink-API/blob/main/en/UIIDProtocol.md)
- [Lokální API brány eWeLink CUBE](https://ewelink.cc/ewelink-cube/introduce-open-api/document/)

Neodvozujeme UIID modelu z názvu. Registrace vyžaduje vlastní zařízení
`itemType=1`, přesnou shodu `deviceid` a `productModel=S60TPF`. Stav musí mít
jednu z publikovaných a skutečně vrácených podob: `switch=on/off`, nebo právě
jeden záznam `switches` s `outlet=0` a `switch=on/off`. Zápis zachovává tuto
pozorovanou podobu a mění pouze zapnutí. Není to tvrzení, že každá verze
firmwaru S60TPF používá stejný protokol. Neznámou či nejednoznačnou podobu
ovladač odmítne před ovládáním; ověření na skutečné zásuvce zbývá.

ON/OFF je poslední stav uložený v cloudu, nikoli přímé místní měření relé.
Po příkazu se provede cílené načtení a porovnání; bez shody je akce neúspěšná.
Příkaz se nikdy automaticky neopakuje. Cloud může vracet starší stav a online
příznak může být opožděný. UI uvádí čas skutečného načtení cloudu.

Měření W/V/A se převádí pouze při skutečně vráceném UIID 5, pro který jsou
jednotky zveřejněné. Moderní, nezdokumentované UIID žádnou předpokládanou
škálu nedostává. Denní a měsíční kWh zůstávají nedostupné; `oneKwhData` není
automaticky dnešní spotřeba. Chybějící hodnoty se zobrazují jako „—“, nikdy 0.
S60TPF tak může být ovladatelná i bez dostupného měření v HanzHubu.

## Co musí dodat správce soukromě

1. Vlastní schválenou aplikaci v [eWeLink Developer Center](https://dev.ewelink.cc/).
   Registrace aplikace vyžaduje název, kontakt, popis a schválenou redirect URL.
   Je potřeba APPID/clientId a clientSecret pro získání OAuth oprávnění.
   Pouhé spárování v mobilní aplikaci ani předplatné Advanced nenahrazuje
   schválení aplikace a oprávnění k danému modelu.
2. OAuth autorizaci účtu vlastníka zásuvky a z ní vydaný `accessToken`, region
   `eu/us/as/cn`. Použij oficiální [OAuth demo](https://github.com/coolkit-carl/eWeLinkOAuthLoginDemo)
   nebo Postman demo odkazované v dokumentaci. Přihlašuj se pouze na oficiální
   autorizaci eWeLink; HanzHub nevyžaduje heslo účtu.
   Ověř návratové `state`; autorizační `code` je podle dokumentace platný 30 s
   a mění se přes `POST /v2/user/oauth/token` s podpisem vlastní aplikace.
   Nepřebírej APPID či tajný klíč z cizích mobilních aplikací.
3. `deviceid` zásuvky z oficiálního `GET /v2/device/thing` po OAuth autorizaci.
   Zkontroluj `productModel`, `extra.uiid` a podobu `params` bez jejich
   zveřejnění; celá odpověď obsahuje i soukromé klíče, nesdílej ji v issue/chatu.

HanzHub za běhu potřebuje jen region, APPID a accessToken. ClientSecret,
heslo účtu, refreshToken a celý výpis zařízení do HanzHubu nevkládej.
Automatické obnovení tokenu není implementované: po expiraci obnov OAuth
podle oficiálního postupu a bezpečně nahraď accessToken. Chyby oprávnění,
kvóty a expirace se zobrazují bez obsahu odpovědi a bez tajných údajů.

## Instalace a přidání zásuvky

Zásuvka už je spárovaná, znovu ji neresetuj. Zkontroluj, že je v eWeLink
online pod účtem, který autorizuješ. Nasazení této změny vyžaduje přestavění
IoT kontejneru z této větve; obecné API i port 4011 zůstávají stejné.

```sh
docker compose up -d --build hanzhub_iot
docker exec -it hanzhub_iot python iot/configure_sonoff.py
```

Druhý příkaz interaktivně vyžádá region, APPID a skrytý vstup accessToken.
Token není argumentem příkazu ani součástí shell historie. Soubor se atomicky
uloží jako `/var/lib/hanzhub-iot/ewelink.json`, s oprávněním 0600 v existujícím
persistujícím volume. Soubor je mimo web, je v `.gitignore` a `.dockerignore`.
Chraň zálohy volume stejně jako účty Tapo a klíče Tuya.
Při obnově tokenu spusť stejný příkaz; ovladač změnu souboru načítá automaticky.
Při samostatném provozu lze cestu nastavit pomocí
`HANZHUB_EWELINK_CREDENTIALS_FILE`; výchozí cestu neměň bez potřeby.

V HanzHubu na nginx portu 4001 otevři existující kartu **IoT moduly**
(samostatná služba 4011) → **Přidat modul** → **SONOFF S60TPF · eWeLink cloud**.
Vyplň název, místnost a `deviceid`. IP, MAC ani TinyTuya klíč nejsou potřeba.
Přidání pouze čte a ověřuje identitu/stav. Cloudové údaje nejsou přes formulář
ani veřejné REST odpovědi přístupné. API zachovává dosavadní omezení na LAN
a stejný Origin; nové cloudové spojení tato omezení neobchází.

## Příchod / Odchod a InfoLCD

Dashboard už inventarizuje moduly podle schopnosti `power`; není potřeba
měnit endpointy scén ani dosavadní výběr BOT/Tapo. V nastavení Dashboardu
vyber SONOFF zvlášť pro **Příchod** (zapnutí) a **Odchod** (vypnutí). Přidání
modulu samo nastavení scén nemění a žádnou scénu nespouští. Volba se ukládá
pod `iot:<interní 12znakové ID modulu>`, nikoli pod ID eWeLink.
Pozastavený nebo odebraný modul se neovládá. Nepotvrzená akce brání označení
celého Odchodu jako úspěšného; ostatní výsledky jsou zachované.

InfoLcd s touto změnou dovoluje stejnou roli `plug` pro Tapo i SONOFF.
Pokud jsou obě zásuvky přidané, ponechá dosavadní volbu Tapo. Pro SONOFF
nastav `plug_id` v `/etc/hanzhub-infopanel.json` na interní ID modulu a restartuj
službu InfoLCD. Nevytváří druhou dlaždici zásuvky; jde o volbu existující role.
Při několika zásuvkách bez uložené volby vyžaduje explicitní `plug_id`.
Neznámé měření zůstává prázdné a nevytváří falešnou historii příkonu.

## Provozní limit a ověření na zařízení

Seznam všech zařízení má společnou dvouminutovou cache (také při chybě).
Pravidelné UI/LCD dotazy nevyvolávají cloudový požadavek při každém obnovení;
`fresh=1` tuto ochranu cloudových čtení neobchází. Cílené čtení před/po příkazu
a zápis mají globální rozestup alespoň 1 s. Běžné trvalé čtení představuje
nejvýše přibližně 22 320 žádostí za 31 dnů; registrace a ovládání přidají další.
Více instancí HanzHubu či aplikací ze stejné veřejné IP se musí koordinovat
samostatně. Oficiální bezplatná APPID má podle dokumentace měsíční kvótu.

Po soukromém nastavení tokenu ověř ručně: přidání nezmění relé; ON/OFF
souhlasí s eWeLink a zásuvkou; nedostupnost není zobrazena jako OFF;
Příchod/Odchod ovládá pouze vybrané moduly a hlásí chybu při nepotvrzeném
SONOFF; BOT a Tapo dále fungují. Fyzické ověření a dostupnost tohoto modelu
pro konkrétní APPID nelze potvrdit bez skutečných OAuth oprávnění a zařízení.
