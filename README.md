# NetMonitor

Nativní desktopový **průhledný widget** pro Windows 10/11, Linux a macOS, který v reálném čase zobrazuje
informace o síťovém připojení (WAN IP, ISP, adaptér, LAN IP, brána, rychlost linky, SSID, DNS) a paralelně
monitoruje dostupnost uživatelsky definovaných IP adres a domén přes ICMP.

- Python 3.12+ · PySide6 (Qt 6) · psutil · SQLite · JSON konfigurace
- Žádný Electron, server, cloud, účet, telemetrie ani placené API
- Nástroje: traceroute, Wake-on-LAN, test rychlosti, nastavení IP adresy karty, automatické aktualizace
- Všechny hodnoty ve widgetu pochází ze skutečného měření/detekce

Rozložení widgetu: [docs/layout.txt](docs/layout.txt)

## Instalace

Hotové balíčky jsou v [Releases](https://github.com/mersl77/netmonitor/releases/latest):

| OS | Soubor | Poznámka |
|---|---|---|
| Windows 10/11 x64 | `NetMonitor-<verze>-setup.exe` | instalace bez práv správce, volitelný autostart; další verze se instalují samy (odznak ⬆) |
| Windows (přenosná) | `NetMonitor-<verze>-win64.zip` | rozbalit a spustit `NetMonitor.exe` |
| Debian / Ubuntu | `netmonitor_<verze>_amd64.deb` | `sudo apt install ./netmonitor_<verze>_amd64.deb` |
| Linux (ostatní) | `NetMonitor-<verze>-linux-x86_64.tar.gz` | rozbalit, `./install.sh` (do `~/.local`) |
| macOS Apple Silicon / Intel | `NetMonitor-<verze>-macos-arm64.dmg` / `-macos-x86_64.dmg` | nepodepsáno – poprvé otevřít přes pravé tlačítko → Otevřít |

**Windows SmartScreen:** instalátor zatím není podepsaný certifikátem pro podpis kódu, proto Windows při prvním
spuštění staženého souboru zobrazí „Systém Windows ochránil váš počítač – Neznámý vydavatel“. Pokračujte přes
*Další informace → Přesto spustit*. Pravost souboru lze ověřit otiskem SHA-256 uvedeným u souboru v Releases
(`Get-FileHash .\NetMonitor-<verze>-setup.exe`). Aktualizace stahované samotnou aplikací se ověřují automaticky.

## Rychlý start (ze zdrojů)

Na Debianu/Ubuntu nejprve nainstalujte systémové knihovny Qt: `sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1`.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
python -m pip install -e .
netmonitor              # nebo:  python -m netmonitor
```

Užitečné přepínače:

| Přepínač | Význam |
|---|---|
| `--minimized` | start skrytě (jen ikona v oznamovací oblasti) |
| `--diagnose` | textová diagnostika (síť, DNS, WAN, ISP, ping všech cílů) do JSON a konec |
| `--diagnose-gui` | prostředí vykreslování do JSON a konec (inicializuje Qt); s `--diagnose` přidá sekci `gui` k síťové diagnostice |
| `--reset-position` | vrátí widget do pravého horního rohu |
| `--debug` | podrobný log |
| `--config CESTA` | jiný konfigurační soubor |

## Sestavení

| OS | Příkaz | Výstup |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1` | `dist\NetMonitor\NetMonitor.exe`, ZIP, volitelně instalátor (Inno Setup 6) |
| Linux | `./scripts/build_linux.sh [--deb] [--appimage]` | `dist/NetMonitor/`, tar.gz, volitelně .deb / AppImage |
| macOS | `./scripts/build_macos.sh [--dmg]` | `dist/NetMonitor.app`, volitelně DMG |

PyInstaller neumí cross-kompilaci – každý OS se sestavuje na sobě. Podrobnosti: [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

## Testy

```bash
pip install -r requirements-dev.txt
pytest            # Linux bez displeje: QT_QPA_PLATFORM=offscreen pytest
```

## Ovládání

- **Levé tlačítko + tah** – přesun (není-li pozice uzamčena)
- **Klik na řádek cíle** – detail s grafem (1 h / 24 h / 7 d / 30 d), statistikami a výpadky
- **Klik na blok síťové karty** (adaptér, LAN IP, brána, DNS) – nastavení IP adresy: DHCP / pevná IP, maska,
  brána, DNS, uložené profily. Vyžaduje práva správce (Windows: dotaz UAC; Linux: NetworkManager/polkit;
  macOS: heslo správce). Před použitím se ověří, zda zadaná IP v síti už neodpovídá (obsazená adresa).
- **Nastavení → Ping cíle** – sekce (např. DNS, DOMÉNY, ZAŘÍZENÍ), řazení přetažením myší nebo šipkami ▲▼,
  nový cíl se vkládá za vybraný řádek
- **Odznak ⬆ vpravo nahoře / v ikoně lišty** – je dostupná nová verze; klik = aktualizace (stažení, ověření
  SHA-256, tichá instalace, restart). Volitelně plně automaticky (Nastavení → Obecné).
- **Pravé tlačítko na řádku cíle** – detail a graf, testovat nyní, **traceroute**, **probudit (Wake-on-LAN)**
- **Test rychlosti internetu** – v menu widgetu i v detailu cíle (stahování, nahrávání, odezva, jitter; servery Cloudflare)
- **Wake-on-LAN** – MAC adresa se zadá u cíle (Nastavení → Ping cíle → Upravit; tlačítko „Zjistit“ ji přečte z ARP)
- **Číslo verze vpravo nahoře** – odkaz na další projekty na [zelina.eu/projekty](https://zelina.eu/projekty.html)
- **Pravé tlačítko** – menu: Aktualizovat (je-li nová verze), Nastavení, Historie, Nastavit IP adresu, průhlednost pozadí, O aplikaci, režim (Na ploše / Vždy navrchu), zámek, click-through,
  průhlednost, velikost, skrytí do oznamovací oblasti, ukončení
- **Úchyt vpravo dole** – změna velikosti
- **Dvojklik na nadpis** – Nastavení
- **Ikona v oznamovací oblasti** – barva = stav připojení; klik = zobrazit/skrýt; menu (zde lze vypnout click-through)

## Kde jsou data

| | Windows | Linux | macOS |
|---|---|---|---|
| Konfigurace | `%APPDATA%\NetMonitor\config.json` | `~/.config/netmonitor/config.json` | `~/Library/Application Support/NetMonitor/` |
| Historie (SQLite), stav WAN | `%LOCALAPPDATA%\NetMonitor\` | `~/.local/share/netmonitor/` | `~/Library/Application Support/NetMonitor/` |
| Logy (`netmonitor.log`, při pádu `crash.log`) | `%LOCALAPPDATA%\NetMonitor\logs\` | `~/.local/share/netmonitor/logs/` | `~/Library/Logs/NetMonitor/` |

Proměnná `NETMONITOR_HOME` přesměruje vše do jedné složky (testy, přenosný režim).

## Dokumentace

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – moduly, vlákna, tok dat
- [docs/PLATFORM_NOTES.md](docs/PLATFORM_NOTES.md) – rozdíly Windows / X11 / Wayland / macOS, ICMP oprávnění
- [docs/EXTERNAL_APIS.md](docs/EXTERNAL_APIS.md) – jaké externí služby a proč (vypnutelné)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) – diagnostika problémů
- [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) – buildy, instalátory, podpis a notarizace
- [docs/STATUS.md](docs/STATUS.md) – co je implementováno, otestováno a co zbývá
- [CHANGELOG.md](CHANGELOG.md) – změny v jednotlivých verzích

## Licence

MIT (kód). Přibalená písma Michroma a Exo 2: SIL Open Font License 1.1 (`src/netmonitor/resources/fonts/OFL-*.txt`).
