# NetMonitor 1.4 — účty, více profilů a synchronizace

> Pracovní dokumentace větve `feature/account-profiles-sync` (13. 11. 2026). Beta 1.4.0b4 implementuje lokální profily a ruční cloudový přenos. Automatická synchronizace zůstává nedokončená. Stabilní v1.3.4 zůstává beze změn.

## Požadované chování

- Jeden účet obsahuje libovolný počet pojmenovaných profilů: např. **Soukromý**, **Firemní**, **Zákazníci**.
- Profil ukládá seznam cílů, skupiny, intervaly, vzhled a oznámení. Každý profil je nezávislý.
- Na různých počítačích může být aktivní jiný profil stejného účtu.
- Přihlášení z nového počítače stáhne dostupné profily. Lokální monitorování zůstává funkční offline.
- Změny se lokálně ukládají okamžitě; synchronizace přenáší změny na vyžádání / při dostupném spojení, konflikty verzí se nesmí tiše přepisovat.
- Stávající uživatelé bez účtu nadále používají lokální konfiguraci.
- Nesynchronizovat identifikaci síťové karty, polohu okna a lokální historii SQLite; uložené interní IP adresy považovat za citlivé.

## Ověřený desktopový projekt

- Repo: https://github.com/mersl77/netmonitor
- Release: v1.3.4; Python 3.12+, PySide6, JSON `config.json`, historie SQLite.
- `src/netmonitor/core/config.py`: `AppConfig`, `PingTarget`, validace, atomické ukládání, import/export.
- `src/netmonitor/gui/controller.py`: `Controller.update_config()` mění konfiguraci a přenačítá cíle monitoringu.
- `src/netmonitor/gui/settings_dialog.py`: `SettingsDialog` vytváří `QTabWidget`, tedy vhodné místo pro záložku **Účet a profily**.
- `src/netmonitor/main.py`: sestavení aplikace a controlleru.

## Dosavadní backend na serveru (ověřeno výstupy správce serveru)

- Host: `aplikacni`, Debian 12 v Proxmox LXC, Docker Compose 5.1.0.
- Projekt: `/opt/netmonitor-sync/`; FastAPI a samostatný PostgreSQL 17.11.
- API: hostitelský bind `127.0.0.1:8766`, `GET /health` vrací 200.
- API je současně připojené do Docker sítí `netmonitor-sync_backend` a `nginx-proxy-manager_default`; DB pouze do backend.
- PostgreSQL tabulky `users` a `profiles` již existují; `user_sessions` existuje zatím jen jako zdrojový model.
- Registrační a přihlašovací endpointy `/auth/register` a `/auth/login` jsou registrované; vypnutá registrace vrací HTTP 403, neexistující login HTTP 401.
- Hesla Argon2id a podpis/ověření HS256 JWT byly funkčně testovány. JWT má platnost 30 minut. **Chybí obnovování relací, revokace, rate limit, ochrana proti zneužití registrace a reálné testování s účty.**
- Sekrety jsou uloženy mimo Git; **nikdy je necommitovat**.
- Backups: `/opt/netmonitor-sync/backups/netmonitor-before-sessions.dump`, katalog ověřen přes `pg_restore --list`; úplná obnova netestována.
- Alembic 1.20.1 nainstalován v nově sestaveném image; soubory `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako` a `alembic/versions/0001_baseline.py` byly vytvořeny na serveru. Zatím žádná databázová migrace nebyla spuštěna.

## Implementační pořadí

1. Bezpečně implementovat účty, session/refresh-token rotaci, revokaci a rate limiting; migrační schéma zavést bez ztráty stávajících tabulek.
2. Profilové API: vytvoření/seznam/stažení/úprava/smazání, validace konfigurace, kontrola vlastnictví každého profilu a optimistic concurrency podle `revision` (409 při konfliktu).
3. Desktopový klient: lokální keyring/token storage, přihlášení, seznam profilů, vytvoření/duplikace/přejmenování/smazání, volba aktivního profilu, přepnutí přes `Controller.update_config()`, lokální cache a offline režim.
4. Testy: izolace profilů různých účtů, neplatné a expirované tokeny, konflikt souběžných úprav, migrace existujícího `config.json`, offline použití, Windows/Linux/macOS build a UI smoke test.
5. Teprve po zabezpečení propojit přes HTTPS / `sync.zelina.eu` a otestovat dva reálné počítače. `main` ani release v1.3.4 před tím neupravovat.

## Stav

- [x] Ověření architektury klienta a připraveného backendu
- [x] Samostatná vývojová větev
- [x] Tato dokumentace
- [x] Desktopová správa profilů a klient existujícího profilového API
- [x] Automatizované testy lokální správy, GUI a cloudového klienta
- [ ] Bezpečné nasazení a test přihlášení na druhém PC

## Použití bety 1.4.0b4

1. Nastavení → Profily → Nový profil. Zadejte např. Domácí nebo Pracovní.
   Profil se vytvoří z aktuální konfigurace a ihned se aktivuje.
2. Ping cíle, intervaly a další nastavení upravujte v dosavadních kartách.
   Uložené IP/DNS předvolby patří do profilu; přepnutí neaplikuje změny IP do OS.
3. Vyberte jiný profil a klikněte na Aktivovat / upravit. Aktuální konfigurace
   se uloží před přepnutím. Aktivní profil nelze smazat, dokud neaktivujete jiný.
4. Přihlaste se na kartě Účet a synchronizace. Na kartě Profily vyberte lokální
   profil a klikněte na Nahrát vybraný lokální profil na server. Opakovaný upload
   aktualizuje spárovaný profil pomocí revize; konflikt 409 nic nepřepíše.
5. Stáhnout vybraný jako nový vytvoří samostatnou kopii a při shodě názvu přidá
   číselnou příponu. Smazat z cloudu ponechá všechny lokální kopie.

Cloud přenáší ping cíle, upozornění a uložené IP/DNS předvolby. Identita adaptéru,
pozice okna, přihlašovací údaje a historie se nepřenášejí. Starší cloudové profily
bez IP/DNS předvoleb zůstávají kompatibilní.

Existující API neumí přejmenování vzdáleného záznamu. Po lokálním přejmenování
vytvoří příští upload novou cloudovou kopii pod novým názvem; původní kopii lze
smazat samostatně. Serverová konfigurace ani databáze se v rámci b4 nemění.
Ověřeno GET /profiles bez tokenu → 401; přihlášený přenos na produkci a mezi dvěma
počítači v této relaci nebyl ověřen. Token zůstává pouze v paměti.
