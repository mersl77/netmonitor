"""Čtení lokálních JSON objektů a odložení poškozených souborů beze ztráty obsahu."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Kořen JSON musí být objekt")
    return data


def quarantine_json(path: Path, error: Exception) -> bool:
    """Odloží originál; při selhání jej ponechá na místě a vrátí False."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex}")
    try:
        path.rename(backup)
    except OSError as e:
        log.warning("Lokální JSON %s nelze načíst (%s) ani odložit (%s); původní soubor zachován",
                    path, error, e)
        return False
    log.warning("Lokální JSON %s nelze načíst (%s); původní soubor odložen do %s",
                path, error, backup)
    return True
