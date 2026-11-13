"""Výběr synchronizovaných nastavení bez přenosu zařízení-specifických údajů."""
from __future__ import annotations

from .config import AppConfig

# Pouze nastavení profilů: žádná síťová konfigurace OS, souřadnice
# oken, přihlašovací údaje nebo historie měření.
_SHARED = ("ping", "notifications")


def export_shared(cfg: AppConfig) -> dict:
    data = cfg.normalized().to_dict()
    return {key: data[key] for key in _SHARED}


def merge_shared(remote: dict, local: AppConfig) -> AppConfig:
    if not isinstance(remote, dict) or not isinstance(remote.get("ping"), dict):
        raise ValueError("Cloudový profil neobsahuje platné nastavení ping")
    # Zachová místní identitu síťové karty, IP profily a umístění widgetu.
    result = local.copy().to_dict()
    for key in _SHARED:
        if key in remote:
            result[key] = remote[key]
    return AppConfig.from_dict(result)
