## 1.4.0b5 (beta)

- **Přepínání profilů** (Soukromý, Firemní, Zákazník…) jedním kliknutím: pravé tlačítko na widgetu nebo na ikoně v liště → Profil. Každý profil má vlastní ping cíle (IP adresy, domény, sekce), upozornění a uložené IP/DNS předvolby. Poloha a velikost okna zůstávají podle počítače.
- **Automatická synchronizace na server**: po přihlášení se profily samy nahrávají a stahují na další počítače (po změně, po startu a každých 5 minut). Přejmenování i smazání se přenáší. Bez přihlášení funguje vše lokálně.
- **Konflikty se neřeší potichu**: když se stejný profil změní na dvou počítačích, aplikace se zeptá – verze z tohoto počítače, verze ze serveru, nebo obě.
- **Registrace licenčním klíčem**: nový účet lze založit jen s klíčem od správce. Klíč má poznámku (pro koho je), počet použití a volitelnou platnost.
- **Karta Správa** (jen správce): vytváření a zneplatnění klíčů, přehled účtů, blokování účtu. Správce se přihlašuje heslem a kódem z autentikátoru (TOTP).
- Přihlášení vydrží restart: na počítači se ukládá jen obnovovací token (Windows: šifrováno DPAPI, Linux/macOS: soubor 0600), heslo nikdy.
- **Nový synchronizační server** ve složce `sync-server` (Docker stack pro Portainer): limity požadavků, zamykání účtu po chybných pokusech, rotace tokenů, klíče uložené jen jako otisk. Nahrazuje rozpracovaný server z b4.

## 1.4.0b4 (beta)

- Karta Profily spojuje pojmenované lokální profily a ruční cloudový přenos.
- Nový profil se ihned aktivuje; úpravy a přepnutí se ukládají bez ztráty ostatních profilů.
- Upload vybraného profilu, download jako nová kopie a smazání cloudové kopie.
- Cloud přenáší ping cíle, upozornění a uložené IP/DNS předvolby; zachovává místní síťovou kartu a pozici okna.
- Souběžné cloudové operace jsou blokovány; stažená data se zapisují v hlavním vlákně.
- Přepnutí profilu neaplikuje IP/DNS do OS; použijte stávající dialog Nastavení IP.
- Ruční synchronizace. Automatická synchronizace není součástí této bety.

# Změny

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
