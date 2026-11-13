# NetMonitor 1.4 – profily, účet a synchronizace

> Stav k betě 1.4.0b6 (13. 11. 2026), větev `feature/account-profiles-sync`. Stabilní vydání je v1.3.4.

## Profily

- Profil = ping cíle (IP adresy, domény, sekce), upozornění a uložené IP/DNS předvolby.
  Příklad: **Soukromý**, **Firemní**, **Zákazníci**.
- Přepnutí: pravé tlačítko na widgetu nebo na ikoně v liště → **Profil**, případně Nastavení → Profily.
- Úpravy v ostatních kartách Nastavení se ukládají do aktivního profilu.
- Přepnutí nemění IP adresu počítače; uloženou IP/DNS předvolbu použijete v dialogu Nastavit IP adresu.
- Poloha a velikost okna a vybraná síťová karta patří počítači, ne profilu.
- Stávající konfigurace se při prvním spuštění převede do profilu „Soukromý“.

## Automatika (Nastavení → Profily)

- **IP/DNS předvolba aktivního profilu:** po přepnutí na profil se aplikace zeptá, zda předvolbu použít,
  a otevře dialog Nastavit IP adresu s načtenými hodnotami. Změnu vždy potvrzuje uživatel (a systém dotazem správce).
  Volba se synchronizuje s profilem.
- **Spojit vybraný profil s touto sítí:** po připojení k dané síti aplikace nabídne přepnutí na profil.
  Wi-Fi se pozná podle názvu (SSID), kabelová síť podle IP a MAC adresy brány. Jedna síť patří jednomu profilu.
  Spojení je uložené jen v tomto počítači. Táž síť se po odmítnutí znovu nenabízí, dokud se síť nezmění.

## Účet a synchronizace

- Bez přihlášení se nic neodesílá.
- Registrace: Nastavení → Účet a synchronizace → e-mail, heslo (min. 10 znaků), licenční klíč od správce.
- Po přihlášení se profily nahrají a dál se synchronizují samy: 4 s po změně, po startu a každých 5 minut.
- Na serveru jsou jen ping cíle, upozornění a IP/DNS předvolby. Historie měření zůstává v počítači.
- Stav každého profilu je vidět v Nastavení → Profily (synchronizováno / čeká na nahrání / konflikt…).

| Situace | Chování |
|---|---|
| Změna na jednom počítači | nahraje se, ostatní počítače si ji stáhnou |
| Změna téhož profilu na dvou počítačích | dotaz: tento počítač / server / ponechat obě |
| První přihlášení na dalším počítači, stejný název a jiný obsah | stejný dotaz |
| Smazání profilu | smaže se i na serveru; na ostatních počítačích zůstane jako „jen místní“ |
| Výpadek spojení | práce pokračuje lokálně, synchronizace se dožene |
| Jiný účet na stejném počítači | profily prvního účtu se do druhého samy nenahrají (tlačítko „Nahrát vybraný do tohoto účtu“) |

Přihlášení přežije restart: ukládá se jen obnovovací token (Windows: DPAPI, Linux/macOS: soubor s právy 0600).

## Správce

- Účet správce vzniká na serveru příkazem `python -m app.cli create-admin` (viz `sync-server/README.md`).
- Po prvním přihlášení karta **Správa** vyžádá zapnutí dvoufázového ověření (TOTP autentikátor, QR kód).
- **Kopírovat pozvánku** u nového klíče zkopíruje zprávu pro uživatele (odkaz ke stažení, postup, klíč).
- Licenční klíč: poznámka pro koho je, počet účtů (výchozí 1), volitelná platnost ve dnech.
  Celý klíč se zobrazí jen při vytvoření.
- Přehled účtů ukazuje, kterým klíčem se kdo registroval; účet lze zablokovat (odhlásí všechna zařízení).

## Kde je kód

| Část | Soubor |
|---|---|
| Místní profily | `src/netmonitor/core/profiles.py` |
| Co se synchronizuje | `src/netmonitor/core/sync_config.py` |
| HTTP klient | `src/netmonitor/core/sync_client.py` |
| Logika synchronizace a konfliktů | `src/netmonitor/core/sync_engine.py` |
| Uložení tokenu | `src/netmonitor/core/secret_store.py` |
| Rozpoznání sítě | `src/netmonitor/core/network_identity.py` |
| QR kód | `src/netmonitor/core/qr.py` (knihovna segno) |
| Dotazy při přepnutí / podle sítě | `src/netmonitor/gui/profile_prompts.py` |
| Propojení s GUI, časování | `src/netmonitor/gui/sync_service.py` |
| Karty Profily / Účet / Správa | `src/netmonitor/gui/account_tabs.py` |
| Server | `sync-server/` |

## Ověřeno / neověřeno

- [x] Automatické testy: klient proti skutečnému kódu serveru (dva počítače, konflikty, klíče, 2FA), GUI offscreen.
- [x] Klient ↔ server přes skutečné HTTP na localhostu (Linux).
- [x] Server nasazen na `sync.zelina.eu` (13. 11. 2026, Docker Compose v `/opt/netmonitor-sync-v2`), přihlášení správce a 2FA z Windows ověřil uživatel.
- [ ] Novinky bety 6 (QR, pozvánka, IP předvolba, profil podle sítě) na skutečném počítači.
- [ ] Dva skutečné počítače (Windows + Linux) proti produkčnímu serveru.
- [ ] Šifrování tokenu DPAPI na Windows ověřuje jen test v CI.
