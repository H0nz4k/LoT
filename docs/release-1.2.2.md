# LoT 1.2.2

- Přímé lokální čtení i potvrzený zápis cílové teploty panelu;
  [mapa DP 1–6](bot-iph2-dps.md) odlišuje skutečně provedené testy.
- Samostatný soubor lokálního klíče mimo Git, načítaný při novém spojení.
- Čtecí diagnostika `probe-panel-dps.py` je součástí instalovaného LAN
  komponentu a jeho manifestu integrity. Neodesílá změny DP.
- Aktualizace nejprve připraví rollback, zachová klíč i ostatní živé
  úpravy a po restartu čeká na skutečnou připravenost HTTP služby.
  Při selhání zdravotní kontroly obnoví předchozí soubory a službu.

Na běžícím HUB2 se nasazuje pouze cílená LAN aktualizace podle
[instalačního postupu](local-lan-control.md). Tag této zdrojové větve
není tvrzením o nasazení všech jejích energetických nebo obnovovacích
funkcí. Aktualizace nemění schválení fyzických povelů ani mapování DP.

## Cena vytápění v současném Dashboardu

Stávající přehled Dashboardu má na kartě napájecí zásuvky infrapanelu
tlačítko **Spotřeba a náklady**, s volbami Den / Týden / Měsíc / Rok.
Výpočet je naměřená energie v kWh × nastavená cena elektřiny;
výchozí sazba je **2,512 Kč/kWh včetně DPH**. Uživatel si ji může změnit
a prohlížeč si ji pamatuje. Částka se tedy řídí právě zobrazenou sazbou.
Historie a obsluha tohoto okna patří do samostatné aplikace Dashboard;
tato LAN aktualizace je zachovává a nepřepisuje.

Pro cenu samotného vytápění je nutné, aby měřená zásuvka napájela pouze
infrapanel. Termostat poskytuje teploty a stav, nikoli energetické kWh.
Chybějící minulá měření nelze odhadovat z nastavené teploty.
