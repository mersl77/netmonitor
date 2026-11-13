"""Výběr synchronizovaných nastavení bez přenosu zařízení-specifických údajů."""
from __future__ import annotations

from .config import AppConfig

# Pouze nastavení profilů: žádná síťová konfigurace OS, souřadnice
# oken, přihlašovací údaje nebo historie měření.
_SHARED = ("ping", "notifications")


def export_shared(cfg: AppConfig) -> dict:
    data = cfg.normalized().to_dict()
    result = {key: data[key] for key in _SHARED}
    result["network"] = {"ip_profiles": data["network"]["ip_profiles"],
                         "switch_ip_profile": data["network"]["switch_ip_profile"]}
    return result


def merge_shared(remote: dict, local: AppConfig) -> AppConfig:
    if not isinstance(remote, dict) or not isinstance(remote.get("ping"), dict):
        raise ValueError("Cloudový profil neobsahuje platné nastavení ping")
    # Zachová místní identitu síťové karty a umístění widgetu.
    # Starší cloudové profily bez IP/DNS předvoleb ponechají místní předvolby.
    result = local.copy().to_dict()
    for key in _SHARED:
        if key in remote:
            result[key] = remote[key]
    network = remote.get("network", {})
    if not isinstance(network, dict):
        raise ValueError("Neplatné síťové nastavení profilu")
    if "ip_profiles" in network:
        if not isinstance(network["ip_profiles"], list):
            raise ValueError("Neplatný seznam IP/DNS nastavení")
        result["network"]["ip_profiles"] = network["ip_profiles"]
    if isinstance(network.get("switch_ip_profile"), str):
        result["network"]["switch_ip_profile"] = network["switch_ip_profile"]
    return AppConfig.from_dict(result)
