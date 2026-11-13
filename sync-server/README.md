# NetMonitor Sync API – integrační patch

Tato složka obsahuje zdrojový modul pro **existující** FastAPI server
na hostu `aplikacni` (`/opt/netmonitor-sync/api/app`).

**Stav:** kód je ve vývojové větvi GitHubu. **Na server nebyl nasazen ani
ověřen s reálnou PostgreSQL databází.** Není součástí desktopového instalátoru.

## Vlastní schéma

Modul `app/profiles.py` předpokládá současné serverové moduly:

- `app.database.get_db` – SQLAlchemy Session z PostgreSQL
- `app.models.User` – UUID primární klíč
- `app.models.Profile` – UUID id, UUID user_id, name, config (JSONB),
  revision, updated_at
- Docker secret `/run/secrets/jwt_secret` s podpisovým klíčem pro
  `/auth/login` (HS256, issuer `netmonitor-sync`,
  audience `netmonitor-desktop`, sub = UUID uživatele)

Po instalaci patchu na serveru je nutné v `app/main.py` přidat:

```python
from .profiles import router as profiles_router
app.include_router(profiles_router)
```

**Neaplikovat bez kontroly skutečného serverového kódu**, modelů,
existujícího JWT payloadu a Alembic revizí. Neměnit produkční databázi
nebo stávající tabulky bez zálohy. `user_sessions` ještě nemá provedenou
migraci; patch ji nepoužívá.

## Endpointy

| HTTP | Cesta | Význam |
|---|---|---|
| POST | `/auth/login` | Existující přihlášení, vrací `access_token` |
| GET | `/profiles` | Seznam vlastních profilů |
| POST | `/profiles` | Nový profil `{name, config}` |
| GET | `/profiles/{uuid}` | Kompletní profil |
| PUT | `/profiles/{uuid}` | Aktualizace `{config,revision}`; 409 při konfliktu |
| DELETE | `/profiles/{uuid}` | Smazání vlastního profilu |

Každý profil má vlastníka z JWT; cizí/neexistující profil vrací 404.
Zápis profilu kontroluje revizi přímo v atomickém databázovém UPDATE.
Heslo není součástí profilu ani synchronizované konfigurace.

## Bezpečnostní nedodělky před zveřejněním na internet

- ochrana proti hrubé síle na `/auth/login` (rate limiting + monitoring);
- refresh tokeny, rotace, revokace relací po odhlášení;
- registrace jen pro správce nebo s pozvánkou;
- kontrola serverového souboru `app/models.py` proti očekávanému schématu,
  reálné API a migrační testy;
- HTTPS přes NPM, přístupová pravidla a test certifikátu;
- jasné omezení validovaných klíčů konfiguračního JSON a kontrola
  chování při mimořádně dlouhých polích.

Dokud tyto kontroly neproběhnou, **nezveřejňovat službu na
`sync.zelina.eu`**. Nový desktopový klient nyní umí ruční přenos, ale
**ne kontinuální automatickou synchronizaci**.


## Založení prvního účtu (nově připraveno v GitHubu)

Na serveru zatím nebyl vytvořen žádný uživatel; registrace přes veřejný
`/auth/register` je standardně vypnutá. **Neexistuje žádné přednastavené
jméno nebo heslo.** Z důvodu ochrany před cizí registrací není správné
nechat endpoint bez omezení otevřený do internetu.

Připraven je `sync-server/bootstrap_user.py`, který:

- načte e-mail a heslo interaktivně (heslo bez zobrazení);
- vyžaduje alespoň 12 znaků;
- hash hesla ukládá pomocí Argon2id;
- dovolí vytvořit pouze **prvního** uživatele, pokud je tabulka prázdná;
- po úspěchu lze tento e-mail a heslo použít v desktopovém přihlášení.

Nejprve je nutné **ověřit** skutečný model `User` a stav kontejneru,
zkopírovat skript do kontejneru API a spustit ho interaktivně s přístupem
k `app.database`. Skript zatím **není nasazen ani spuštěn**.
Nikdy nepřidávat heslo do Compose, Git repozitáře ani shell historie.

Toto řeší **první účet**, nikoli samoobslužnou registraci dalších
uživatelů. Pro další účty bude potřeba pozvánkový mechanismus,
administrační API nebo samostatný bezpečný administrační příkaz.
E-mailové ověření a obnova hesla dosud nejsou implementovány.
