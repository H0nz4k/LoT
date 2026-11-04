# Tapo C320WS v HanzHub IoT

Nový ovladač `tapo_c320ws` používá pouze místní RTSP na portu 554. V UI zvol **Přidat modul → TP-Link Tapo C320WS**, vyplň IP a přihlašovací jméno a heslo účtu **Camera Account**. Ten vytvoříš v mobilní aplikaci Tapo v nastavení kamery (Pokročilá nastavení → Účet kamery); **nejde o cloudový TP-Link e-mail/heslo**. Kamera musí být v LAN přístupná z HUBu.

Na detailu kamery se zobrazují JPEG snímky z `/stream2`, obnovované přibližně po 10 sekundách prostřednictvím serverové proxy `GET /api/iot/devices/{id}/snapshot`. RTSP přihlašovací údaje ani URL se neposílají do prohlížeče. Snímky jsou dostupné pouze klientům splňujícím stejné LAN omezení jako ostatní IoT API.

**Rozsah:** fotografie, nikoli plynulé video; bez ONVIF PTZ, audia, nahrávání či detekce pohybu. Přidání modulu kontroluje otevřený RTSP port, nikoli správnost hesla; to se projeví až při prvním načtení snímku. Docker obraz instaluje ffmpeg. Stávající Tuya/Tapo P110M integrace zůstávají beze změny. Komunikace s reálnou kamerou nebyla při vývoji ověřena.
