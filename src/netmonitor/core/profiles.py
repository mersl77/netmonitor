"""Lokální pojmenované profily NetMonitoru.

Každý profil je úplný snapshot AppConfig. Aktivní konfigurace zůstává také
v původním config.json kvůli kompatibilitě se staršími verzemi aplikace.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import AppConfig, atomic_write_json
from ..utils.paths import config_dir

INDEX = "profile-index.json"


class ProfileStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config_dir() / "profiles"
        self.index_path = self.root / INDEX

    def _read(self) -> dict:
        with self.index_path.open(encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("profiles", [])
        if not isinstance(entries, list) or not entries:
            raise ValueError("Neplatný seznam profilů")
        ids = [p["id"] for p in entries]
        for value in ids:
            uuid.UUID(value)  # vyloučení průchodu adresáři
        if len(set(ids)) != len(ids) or data.get("active") not in ids:
            raise ValueError("Neplatný aktivní profil nebo duplicitní ID")
        return data

    def _file(self, profile_id: str) -> Path:
        return self.root / f"{uuid.UUID(profile_id)}.json"

    def initialize(self, current: AppConfig) -> None:
        """Převede původní nastavení do prvního profilu bez jeho ztráty."""
        if self.index_path.exists():
            self._read()
            return
        profile_id = str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._file(profile_id), current.to_dict())
        atomic_write_json(self.index_path, {
            "schema_version": 1, "active": profile_id,
            "profiles": [{"id": profile_id, "name": "Soukromý"}],
        })

    def list_profiles(self) -> list[dict]:
        return [dict(item) for item in self._read()["profiles"]]

    def active_id(self) -> str:
        return self._read()["active"]

    def record(self, profile_id: str) -> dict:
        for item in self._read()["profiles"]:
            if item["id"] == profile_id:
                return dict(item)
        raise ValueError("Neznámý profil")

    def bind_remote(self, profile_id: str, remote_id: str, revision: int,
                    account: str, server: str) -> None:
        """Spáruje lokální profil se vzdáleným po potvrzeném úspěšném zápisu."""
        uuid.UUID(remote_id)
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("Neplatná revize")
        index = self._read()
        for item in index["profiles"]:
            if item["id"] == profile_id:
                item["remote_id"] = remote_id
                item["remote_revision"] = revision
                item["remote_account"] = account.strip().casefold()
                item["remote_server"] = server.rstrip("/")
                atomic_write_json(self.index_path, index)
                return
        raise ValueError("Neznámý profil")

    def load(self, profile_id: str) -> AppConfig:
        if profile_id not in {p["id"] for p in self.list_profiles()}:
            raise ValueError("Neznámý profil")
        with self._file(profile_id).open(encoding="utf-8") as f:
            return AppConfig.from_dict(json.load(f))

    def save(self, profile_id: str, cfg: AppConfig) -> None:
        if profile_id not in {p["id"] for p in self.list_profiles()}:
            raise ValueError("Neznámý profil")
        atomic_write_json(self._file(profile_id), cfg.to_dict())

    def save_active(self, cfg: AppConfig) -> None:
        self.save(self.active_id(), cfg)

    def create(self, name: str, cfg: AppConfig) -> str:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("Název musí mít 1 až 100 znaků")
        index = self._read()
        if any(p["name"].casefold() == name.casefold() for p in index["profiles"]):
            raise ValueError("Profil s tímto názvem už existuje")
        profile_id = str(uuid.uuid4())
        atomic_write_json(self._file(profile_id), cfg.to_dict())
        index["profiles"].append({"id": profile_id, "name": name})
        atomic_write_json(self.index_path, index)
        return profile_id

    def rename(self, profile_id: str, name: str) -> None:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("Název musí mít 1 až 100 znaků")
        index = self._read()
        if any(p["name"].casefold() == name.casefold() and p["id"] != profile_id
               for p in index["profiles"]):
            raise ValueError("Profil s tímto názvem už existuje")
        for item in index["profiles"]:
            if item["id"] == profile_id:
                item["name"] = name
                atomic_write_json(self.index_path, index)
                return
        raise ValueError("Neznámý profil")

    def activate(self, profile_id: str) -> AppConfig:
        cfg = self.load(profile_id)  # nejprve ověřit, že snapshot je čitelný
        index = self._read()
        index["active"] = profile_id
        atomic_write_json(self.index_path, index)
        return cfg

    def delete(self, profile_id: str) -> None:
        index = self._read()
        if profile_id == index["active"]:
            raise ValueError("Aktivní profil nelze smazat")
        if profile_id not in {p["id"] for p in index["profiles"]}:
            raise ValueError("Neznámý profil")
        index["profiles"] = [p for p in index["profiles"] if p["id"] != profile_id]
        atomic_write_json(self.index_path, index)
        self._file(profile_id).unlink(missing_ok=True)
