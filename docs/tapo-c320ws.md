# Tapo C320WS v HanzHub IoT

Nový ovladač `tapo_c320ws` používá pouze místní RTSP na portu 554. V UI zvol **Přidat modul → TP-Link Tapo C320WS**, vyplň IP a přihlašovací jméno a heslo účtu **Camera Account**. Ten vytvoříš v mobilní aplikaci Tapo v nastavení kamery (Pokročilá nastavení → Účet kamery); **nejde o cloudový TP-Link e-mail/heslo**. Kamera musí být v LAN přístupná z HUBu.

Na detailu kamery se zobrazují JPEG snímky z `/stream2`, obnovované přibližně po 10 sekundách prostřednictvím serverové proxy `GET /api/iot/devices/{id}/snapshot`. Tlačítko **Živě** pustí plynulé video stejného substreamu: prohlížeč dostane jen MSE přes WebSocket na `/api/iot/devices/{id}/live` (případně WebRTC v domácí síti). RTSP adresu i heslo drží server a místní go2rtc na `127.0.0.1:1984`; do prohlížeče, Compose ani gitu se nedostanou. Když se video nespustí, karta zůstane u snímku a vypíše českou chybu.

**Rozsah:** snímek a živý náhled; bez ONVIF PTZ, nahrávání či detekce pohybu. Přidání modulu kontroluje otevřený RTSP port, nikoli správnost hesla; to se projeví až při prvním snímku nebo živém videu. Docker obraz IoT instaluje ffmpeg, vedle něj běží `alexxit/go2rtc`. Stávající Tuya/Tapo P110M integrace zůstávají beze změny.
