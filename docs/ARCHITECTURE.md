# Architektura

```
src/netmonitor/
├── main.py                 vstup, argumenty, single-instance zámek, sestavení aplikace
├── core/                   čistý Python BEZ Qt – plně testovatelné
│   ├── config.py           dataclassy konfigurace, validace, JSON (atomický zápis), import/export, migrace
│   ├── ping_engine.py      ICMP: Windows IcmpSendEcho / ICMP socket / systémový ping; DNS resolver s cache
│   ├── monitoring.py       plánovač (monotonic, fixed-rate), daemon pool, statistiky, přechody UP/DOWN
│   ├── connection_status.py vyhodnocení ONLINE/DEGRADED/OFFLINE/UNKNOWN po vrstvách
│   ├── network_info.py     aktivní rozhraní (výchozí trasa), LAN IP, brána, rychlost linky, Wi-Fi, DNS
│   ├── wan_info.py         WAN IPv4 (4 poskytovatelé s fallbackem), ISP (ipinfo / RIPEstat), cache, změny
│   ├── history.py          SQLite (WAL), minutové agregace, dávkový zápis, události, migrace, CSV export
│   ├── notifier.py         logika upozornění: prodleva, cooldown, bez zahlcení
│   ├── target_order.py     sekce a pořadí cílů (vkládání, přesuny bloků)
│   ├── ipconfig.py         nastavení IP karty: validace, profily, příkazy netsh / nmcli / networksetup
│   ├── updater.py          kontrola a stažení aktualizace (GitHub Releases / JSON manifest, SHA-256)
│   ├── traceroute.py       tracert / traceroute / tracepath v samostatném procesu, parsování po skocích
│   ├── wol.py              Wake-on-LAN (magic packet), MAC z ARP tabulky
│   └── speedtest.py        test rychlosti (Cloudflare), souběžná spojení, odezva a jitter
├── platform/               OS-specifické adaptéry
│   ├── windows.py          GetAdaptersAddresses (ctypes), netsh wlan, HKCU\…\Run, HWND_BOTTOM
│   ├── linux.py            /proc/net/route, resolvectl/nmcli/resolv.conf, nmcli/iw, XDG autostart
│   └── macos.py            route/scutil/ipconfig, LaunchAgent
├── gui/                    PySide6
│   ├── controller.py       most jádro ↔ Qt: úlohy na pozadí, queued signály, ukládání konfigurace
│   ├── main_widget.py      průhledné okno, režimy, přesun/zámek/velikost, kontextové menu
│   ├── network_panel.py    horní část (síťové informace)
│   ├── ping_panel.py       dolní část (řádky cílů – každý se překresluje samostatně)
│   ├── settings_dialog.py  nastavení (6 záložek), živé promítání změn
│   ├── history_dialog.py   graf, statistiky, události, export CSV, nástroje cíle
│   ├── ipconfig_dialog.py  nastavení IP adresy karty (DHCP / pevná IP / DNS, profily)
│   ├── updater_ui.py       odznak a dialog aktualizace, O aplikaci
│   ├── tools_dialog.py     okna traceroute a testu rychlosti, Wake-on-LAN
│   ├── tray.py             ikona v oznamovací oblasti, notifikace
│   ├── theme.py · widgets.py · icons.py · i18n.py
└── resources/              písma (OFL), ikony
```

## Vlákna a tok dat

```
 Qt hlavní vlákno (UI)  ◄── queued Signal ──┐
                                            │
 ping-scheduler (1 vlákno) ── submit ──► DaemonPool "ping_*" (max_concurrent, výchozí 16)
        │  heap (due, target)                 │ ping_host(): resolver → pinger (DNS nemá deadline)
        │  monotonic clock                    ▼
        │                               TargetStats.add() pod zámkem → snapshot → callback
 DaemonPool "bg_*" (3)  ← síťové info (každých 5 s), WAN/ISP (5 min), flush DB (60 s), zápis konfigurace
```

- **I/O controlleru za běhu probíhá v pozadí.** Výjimkou je synchronní poslední zápis konfigurace při ukončení.
- **Žádné překrývání** požadavků na stejný cíl (`_inflight`), žádná dávka "dohánění" po zpoždění.
- **Uspání/probuzení**: skok wall-clock vs. monotonic → vymazání DNS cache, přeplánování, obnovení WAN/sítě.
- **Změna sítě** (rozhraní/IP/brána/DNS/SSID) → totéž + přepnutí sondy brány.
- **Konfigurace**: debounce 600 ms, revize se označují čítačem; po dokončení staršího zápisu se uloží novější
  požadavek. Zápisy jsou serializované a atomické (`fsync` + `os.replace`).
- **Ukončení**: odmítnutí nových úloh, skrytí widgetu a zastavení časovačů → synchronní zápis poslední požadované
  revize konfigurace → zrušení čekajících úloh v pozadí → zahájení úklidu historie → `engine.stop(timeout)`.
  Historie tak flushuje souběžně s čekáním na visící síťové dotazy. Join scheduleru, aktuálního i vyřazených
  ping poolů a úklidu historie sdílí rozpočet 2 s (cíl celého ukončení ≤ 3 s). Scheduler při každém submitu
  vybírá aktuální pool pod zámkem, takže živá změna limitu souběhu nepřeruší plánování.
  Workery jsou daemon a nemají atexit join standardního `ThreadPoolExecutor`, takže ani visící resolver
  nezadrží konec Pythonu. Hotové callbacky se ve workerech nezadržují. Log obsahuje „Ukončuji…“ a trvání v sekundách.
  Historie se uzavře po dokončení již běžících DB úloh; při překročení rozpočtu se zaloguje varování a úklid
  pokračuje jako daemon. Při konci procesu se v takovém případě nemusí uložit poslední agregáty historie.
- **Výjimky z časového cíle**: již běžící `apply_ip` se nepřerušuje uprostřed sekvence. Shutdown zaloguje čekání
  a počká na konec této jediné operace podle existujících platformních timeoutů (včetně autorizace).
  Výjimka při čekání se zaloguje a nezabrání zastavení monitoringu ani úklidu historie.
  Dosud nezačatá změna IP se zruší. Ani synchronní zápis konfigurace nemůže garantovat limit při visícím disku;
  chyba zápisu se zaloguje a ukončení pokračuje. Systémový ping používá `subprocess.run(timeout)`.

## Vyhodnocení stavu připojení

1. Rozhraní bez IP / bez výchozí trasy → **OFFLINE**
2. Internetové cíle = veřejné adresy (lze přepsat u cíle). Všechny dole a ≥ 2 cíle → **OFFLINE**;
   jen jeden cíl → **DEGRADED** (výpadek nelze potvrdit); ICMP mlčí, ale HTTPS (WAN dotaz) funguje → **DEGRADED**
3. ≥ 1/3 internetových cílů dole, medián latence > oranžový práh, medián ztrátovosti ≥ 20 % → **DEGRADED**
4. Jinak **ONLINE**. Brána neodpovídající na ping (časté blokování ICMP) online stav nekazí.

## Historie

Ping každé 2 s × 10 cílů = 432 000 vzorků/den. Místo toho se ukládá **1 řádek / cíl / minuta**
(min, avg, max, OK, FAIL, poslední stav a chyba) jedním dávkovým UPSERTem za minutu → ~14 400 řádků/den.
Graf „1 hodina“ používá surové vzorky z paměťového ring-bufferu. Události (výpadek, obnovení, změna WAN,
internet) jsou v samostatné tabulce a zapisují se okamžitě.
