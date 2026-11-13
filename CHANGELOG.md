# Změny

## 1.4.0
- **Profily** (Soukromý, Firemní, Zákazník…): každý má vlastní ping cíle (IP adresy, domény, sekce), upozornění a uložené IP/DNS předvolby. Přepnutí jedním kliknutím – pravé tlačítko na widgetu nebo na ikoně v liště → Profil. Stávající nastavení se převede do profilu „Soukromý“.
- **Účet a automatická synchronizace** (volitelné, vlastní server): po přihlášení se profily samy nahrávají a stahují na další počítače. Když se stejný profil změní na dvou místech, aplikace se zeptá, kterou verzi zachovat. Bez přihlášení funguje vše lokálně jako dosud.
- **Registrace licenčním klíčem**: nový účet lze založit jen s klíčem od správce serveru.
- **Karta Správa** (jen správce): licenční klíče s poznámkou, počtem použití a platností, tlačítko Kopírovat pozvánku, přehled a blokování účtů. Správce se přihlašuje heslem a kódem z autentikátoru (nastavení QR kódem).
- **IP/DNS předvolba profilu**: po přepnutí profilu aplikace nabídne použití přiřazené předvolby; změnu vždy potvrzuje uživatel.
- **Profil podle sítě**: profil lze spojit se sítí (Wi-Fi podle názvu, kabel podle brány); po připojení aplikace nabídne přepnutí.
- Přihlášení vydrží restart; na počítači se ukládá jen přihlašovací token (Windows: šifrováno), heslo nikdy.
- Volba „Přijímat beta aktualizace“ v Nastavení → Obecné.
- Synchronizační server je ve složce `sync-server` (Docker), viz `sync-server/README.md`.

## 1.3.4
- Linux: když XWayland (Qt xcb) nejde načíst – typicky chybí libxcb-cursor0 –, aplikace na Waylandu místo pádu nastartuje nativně a v Nastavení i v logu řekne, co doinstalovat (#1)
- Linux: položka v menu (.desktop) má jen jednu hlavní kategorii (Network), v některých prostředích se už nezobrazí dvakrát (#2)
- Linux balíčky: build ověří, že bundle obsahuje knihovny Qt xcb (libxcb-cursor, libxkbcommon-x11); .deb navíc závisí na libgl1, doporučuje traceroute a nabízí network-manager; install.sh upozorní na chybějící knihovny a ikonu ukládá ve správné velikosti 512×512 (#3)

## 1.3.3
- Oprava skutečné příčiny pádu po traceroute: okno traceroute nemělo trvalý odkaz a po doběhnutí trasy ho Python rušil mimo hlavní vlákno, což shodilo celou aplikaci

## 1.3.2
- Oprava: zapnutí/vypnutí cíle zaškrtávátkem v Nastavení mohlo shodit aplikaci (strom se překresloval uprostřed zpracování kliknutí)

## 1.3.1
- Oprava: traceroute na Windows mohl po několika skocích shodit celou aplikaci. Nově běží přes systémový `tracert` v samostatném procesu.
- Větší rezerva pro odpovědi ICMP na Windows (chybové odpovědi routerů s citovaným paketem)
- Při případném tvrdém pádu se zapíše `logs/crash.log` (výpis všech vláken) pro snadnou diagnostiku
- Číslo verze s odkazem na zelina.eu/projekty přesunuto do horního pravého „tabu“ rámečku widgetu

## 1.3.0
- Wake-on-LAN: pravým tlačítkem na cíl ve widgetu → Probudit; MAC adresa u cíle (tlačítko „Zjistit“ ji přečte z ARP)
- Traceroute k cíli s živou tabulkou skoků
- Test rychlosti internetu (stahování, nahrávání, odezva, jitter) – menu widgetu i detail cíle
- Pravé tlačítko na řádku cíle: detail a graf, testovat nyní, traceroute, probudit
- Výklenek s číslem verze ve spodní hraně widgetu – odkaz na zelina.eu/projekty

## 1.2.0
- Automatické aktualizace: odznak nové verze ve widgetu a v ikoně lišty, jedním klikem stažení (ověření SHA-256), tichá instalace a restart; volitelně plně automaticky
- Výchozí neprůhledné pozadí, posuvník průhlednosti pozadí 0–100 % (Nastavení → Vzhled i pravé tlačítko)
- O aplikaci s odkazem na zelina.eu/projekty

## 1.1.0
- Sekce ping cílů (např. DNS, DOMÉNY, ZAŘÍZENÍ) – nadpisy ve widgetu i v nastavení
- Volné řazení cílů přetažením myší nebo šipkami, nový cíl se vkládá za vybraný řádek
- Nastavení IP adresy síťové karty kliknutím na blok síťové karty (DHCP / pevná IP / DNS, profily, kontrola obsazené IP)

## 1.0.1
- Lepší čitelnost na světlé tapetě, zmenšování widgetu až na 260 × 340, zkracování dlouhých textů
- Oprava rychlosti linky > 4,29 Gb/s na Windows, UTF-8 BOM pro skripty Windows
