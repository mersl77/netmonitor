"""Jednoduchá lokalizace. Zdrojový jazyk je čeština; angličtina je připravená (slovník EN).

Použití: ``tr("Nastavení")``. Chybějící překlad => vrátí se český text.
Pro plnohodnotné Qt Linguist (.ts/.qm) lze tr() později napojit na QCoreApplication.translate.
"""
from __future__ import annotations

_lang = "cs"

EN: dict[str, str] = {
    "SÍŤOVÉ INFORMACE": "NETWORK INFO",
    "Nastavení…": "Settings…",
    "Historie…": "History…",
    "Nastavit IP adresu…": "Set IP address…",
    "Probudit zařízení…": "Wake device…",
    "Uložená zařízení WoL": "Saved WoL devices",
    "(žádná zařízení)": "(no devices)",
    "Režim zobrazení": "Display mode",
    "Na ploše (pod okny)": "On desktop (below windows)",
    "Vždy navrchu": "Always on top",
    "Uzamknout pozici": "Lock position",
    "Ignorovat myš (click-through)": "Click-through",
    "Průhlednost": "Opacity",
    "Průhlednost pozadí": "Background transparency",
    "O aplikaci…": "About…",
    "Test rychlosti internetu…": "Internet speed test…",
    "Velikost": "Size",
    "Kompaktní": "Compact",
    "Výchozí (600 × 850)": "Default (600 × 850)",
    "Velký": "Large",
    "Obnovit nyní": "Refresh now",
    "Skrýt do oznamovací oblasti": "Hide to tray",
    "Zobrazit widget": "Show widget",
    "Ukončit": "Quit",
    "Linková rychlost adaptéru – není to rychlost internetu": "Adapter link rate – not internet speed",
    "Zatím neměřeno": "Not measured yet",
}


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in ("cs", "en") else "cs"


def language() -> str:
    return _lang


def tr(text: str) -> str:
    if _lang == "en":
        return EN.get(text, text)
    return text
