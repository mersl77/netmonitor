"""Lokální potvrzení pádu podle růstu crash.log, bez závislosti na Qt."""
from __future__ import annotations

import json
import logging

from ..core.config import atomic_write_json
from .paths import data_dir, log_dir

log = logging.getLogger(__name__)


def mark_seen(size: int) -> None:
    """Atomicky potvrdí zjištěnou velikost; chyba nesmí zastavit GUI."""
    try:
        atomic_write_json(data_dir() / "crash-notice.json", {"seen_size": size})
    except OSError:
        log.warning("Potvrzení crash.log se nepodařilo uložit", exc_info=True)


def mark_clean_exit() -> None:
    """Potvrdí aktuální velikost logu po normálním ukončení GUI."""
    try:
        mark_seen((log_dir() / "crash.log").stat().st_size)
    except OSError:
        log.warning("Velikost crash.log při ukončení se nepodařilo zjistit", exc_info=True)


def pending_crash() -> int | None:
    """Vrátí nepotvrzenou velikost; první start, neplatná značka a zkrácení ji srovnají."""
    try:
        try:
            size = (log_dir() / "crash.log").stat().st_size
        except FileNotFoundError:
            size = 0
        try:
            marker = json.loads((data_dir() / "crash-notice.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            mark_seen(size)
            return None
        except (ValueError, RecursionError):
            log.warning("Neplatná značka potvrzení crash.log", exc_info=True)
            mark_seen(size)
            return None
        if (not isinstance(marker, dict) or type(marker.get("seen_size")) is not int
                or marker["seen_size"] < 0):
            log.warning("Neplatná značka potvrzení crash.log")
            mark_seen(size)
            return None
        seen = marker["seen_size"]
        if size < seen:
            mark_seen(size)
        elif size > seen:
            return size
    except (OSError, ValueError):
        log.warning("Kontrola crash.log se nepodařila", exc_info=True)
    return None
