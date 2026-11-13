# NetMonitor Sync – server pro účty, licenční klíče a profily

Samostatný server (FastAPI + PostgreSQL), se kterým NetMonitor 1.4 synchronizuje profily.
Image: `ghcr.io/mersl77/netmonitor-sync` (sestavuje GitHub Actions z této složky).

**Stav:** kód je pokrytý testy (server samostatně i s desktopovým klientem). Nasazení na
`sync.zelina.eu` provádí správce podle postupu níže – z vývojové relace nasazeno nebylo.

## Co server hlídá

| Ochrana | Jak |
|---|---|
| Registrace | jen s platným licenčním klíčem; `REGISTRATION_ENABLED=false` ji vypne úplně |
| Licenční klíč | 100 bitů náhody, v databázi jen otisk SHA-256, počet použití, volitelná platnost, zneplatnění |
| Zaplavení požadavky | limit na IP: 240 požadavků/min celkem, 10 přihlášení/5 min, 5 registrací/hod (HTTP 429) |
| Hádání hesla | Argon2id; po 5 chybných pokusech se účet na 15 minut zamkne |
| Správce | správa klíčů a účtů jen s dvoufázovým ověřením (TOTP), kód nelze použít dvakrát |
| Relace | přístupový token 15 min, obnovovací token se při každém použití vymění; použití starého tokenu relaci ukončí |
| Data | profil vidí jen jeho účet (ani správce ne); max. 20 profilů na účet, 512 KiB na profil |
| Souběh | zápis profilu kontroluje revizi; konflikt = HTTP 409, nic se nepřepíše |

## Nasazení přes SSH (Docker Compose)

Takto je nasazen `sync.zelina.eu` (složka `/opt/netmonitor-sync-v2`):

```sh
( set -e
mkdir -p /opt/netmonitor-sync-v2 && cd /opt/netmonitor-sync-v2
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/mersl77/netmonitor/main/sync-server/docker-compose.yml
if [ ! -f .env ]; then printf 'POSTGRES_PASSWORD=%s\nJWT_SECRET=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 32)" > .env; fi
chmod 600 .env
docker compose up -d --wait
curl -s http://127.0.0.1:8766/health )
docker compose exec api python -m app.cli create-admin
```

Aktualizace serveru: `cd /opt/netmonitor-sync-v2 && docker compose pull && docker compose up -d --wait`.

## Nasazení v Portaineru

1. **Stacks → Add stack → Web editor**, název `netmonitor-sync`, vložit obsah `docker-compose.yml`.
2. **Environment variables**:

   | Název | Hodnota |
   |---|---|
   | `POSTGRES_PASSWORD` | náhodné heslo, např. výstup `openssl rand -hex 24` |
   | `JWT_SECRET` | min. 32 znaků, např. výstup `openssl rand -hex 32` |
   | `PROXY_NETWORK` | Docker síť reverzní proxy (výchozí `nginx-proxy-manager_default`) |
   | `API_BIND` | volitelné, výchozí `127.0.0.1:8766` |

3. **Deploy the stack.** Kontrola na hostiteli: `curl -s http://127.0.0.1:8766/health`.
4. V Nginx Proxy Manageru vytvořit Proxy Host `sync.zelina.eu` → `netmonitor-sync-api`, port `8000`
   (schéma http), zapnout SSL (Let's Encrypt, Force SSL).
5. **První správce** – Portainer → kontejner `api` → Console (`/bin/sh`):

   ```sh
   python -m app.cli create-admin
   ```

   E-mail a heslo se zadávají interaktivně (heslo skrytě, nikam se neloguje).
6. V NetMonitoru: Nastavení → Účet a synchronizace → přihlásit se → karta **Správa** →
   zapnout dvoufázové ověření. Teprve potom jde vytvářet licenční klíče.

Stack používá vlastní svazek `netmonitor-sync_db-data`. Rozpracovaný server z bety 1.4.0b4
(`/opt/netmonitor-sync`) má jiné schéma tabulek – nový stack nasazujte s čistou databází a starý vypněte.

## Správa z příkazové řádky (Console kontejneru `api`)

```sh
python -m app.cli create-admin                 # nový správce / povýšení existujícího účtu
python -m app.cli create-key "Pavel – kancelář" --days 30
python -m app.cli list-users
python -m app.cli reset-password uzivatel@domena.cz
python -m app.cli reset-totp spravce@domena.cz # ztracený telefon správce
```

## API

| Metoda | Cesta | Význam |
|---|---|---|
| GET | `/health` | stav serveru |
| POST | `/auth/register` | `{email, password, license_key}` |
| POST | `/auth/login` | `{email, password, totp_code?}` → přístupový a obnovovací token |
| POST | `/auth/refresh`, `/auth/logout`, `/auth/password` | obnovení, odhlášení, změna hesla |
| POST | `/auth/totp/setup`, `/auth/totp/enable` | zapnutí dvoufázového ověření |
| GET/POST | `/profiles` | seznam / nový profil |
| GET/PUT/DELETE | `/profiles/{id}` | čtení / zápis s revizí (i přejmenování) / smazání |
| GET/POST | `/admin/keys`, `/admin/keys/{id}/revoke` | licenční klíče (správce s 2FA) |
| GET/POST | `/admin/users`, `/admin/users/{id}/disable\|enable` | účty (správce s 2FA) |

Chyby mají tvar `{"detail": {"code": "...", "message": "..."}}`.

## Vývoj a testy

```sh
pip install -r requirements-dev.txt
python -m pytest -q tests
JWT_SECRET=$(openssl rand -hex 32) DATABASE_URL=sqlite:///dev.sqlite3 uvicorn app.asgi:app --port 8766
```

## Známá omezení

- Limit požadavků je v paměti jednoho procesu (po restartu kontejneru se nuluje).
- Schéma vzniká přes `create_all`; migrační nástroj (Alembic) zatím není potřeba ani nasazen.
- Obnova zapomenutého hesla e-mailem není – heslo resetuje správce příkazem `reset-password`.
- Zálohování databáze (pg_dump svazku) si řeší správce serveru.
