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
                    account: str, server: str, synced_hash: str | None = None) -> None:
        """Spáruje lokální profil se vzdáleným po potvrzeném úspěšném zápisu.

        synced_hash je otisk obsahu, který je v tu chvíli shodný se serverem; podle něj se
        pozná, že se profil od poslední synchronizace místně změnil.
        """
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
                item["synced_hash"] = synced_hash or ""
                item.pop("local_only", None)
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
                item["name"] = name      # vazba na server zůstává; nový název se nahraje při synchronizaci
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
        gone = next(p for p in index["profiles"] if p["id"] == profile_id)
        if gone.get("remote_id"):
            # Smazání se na serveru dokončí při nejbližší synchronizaci (i když je teď počítač offline).
            index.setdefault("pending_deletes", []).append({
                "remote_id": gone["remote_id"], "account": gone.get("remote_account", ""),
                "server": gone.get("remote_server", "")})
        index["profiles"] = [p for p in index["profiles"] if p["id"] != profile_id]
        atomic_write_json(self.index_path, index)
        self._file(profile_id).unlink(missing_ok=True)

    def pending_deletes(self, account: str, server: str) -> list[str]:
        return [d["remote_id"] for d in self._read().get("pending_deletes", [])
                if d.get("account") == account and d.get("server") == server]

    def clear_pending_delete(self, remote_id: str) -> None:
        index = self._read()
        index["pending_deletes"] = [d for d in index.get("pending_deletes", []) if d.get("remote_id") != remote_id]
        atomic_write_json(self.index_path, index)

    def unbind(self, profile_id: str, local_only: bool = False) -> None:
        """Zruší vazbu na server. local_only=True: profil se už sám nenahrává."""
        index = self._read()
        for item in index["profiles"]:
            if item["id"] == profile_id:
                for key in [k for k in item if k.startswith("remote_") or k == "synced_hash"]:
                    del item[key]
                if local_only:
                    item["local_only"] = True
                else:
                    item.pop("local_only", None)
                atomic_write_json(self.index_path, index)
                return
        raise ValueError("Neznámý profil")

    # -- spojení profilu se sítí (jen pro tento počítač, nesynchronizuje se) --
    def bind_network(self, profile_id: str, key: str, label: str) -> None:
        """Jedna síť patří nejvýš jednomu profilu."""
        if not key:
            raise ValueError("Síť se nepodařilo rozpoznat")
        index = self._read()
        if profile_id not in {p["id"] for p in index["profiles"]}:
            raise ValueError("Neznámý profil")
        for item in index["profiles"]:
            nets = [n for n in item.get("networks", []) if n.get("key") != key]
            if item["id"] == profile_id:
                nets.append({"key": key, "label": label[:80]})
            if nets:
                item["networks"] = nets
            else:
                item.pop("networks", None)
        atomic_write_json(self.index_path, index)

    def unbind_networks(self, profile_id: str) -> None:
        index = self._read()
        for item in index["profiles"]:
            if item["id"] == profile_id:
                item.pop("networks", None)
        atomic_write_json(self.index_path, index)

    def profile_for_network(self, key: str) -> str | None:
        for item in self._read()["profiles"]:
            if any(n.get("key") == key for n in item.get("networks", [])):
                return item["id"]
        return None

    def unique_name(self, name: str, skip_id: str | None = None) -> str:
        taken = {p["name"].casefold() for p in self.list_profiles() if p["id"] != skip_id}
        name = name.strip()[:100] or "Profil"
        candidate, i = name, 2
        while candidate.casefold() in taken:
            suffix = f" ({i})"
            candidate = name[:100 - len(suffix)] + suffix
            i += 1
        return candidate

    def unbind_remote(self, remote_id: str, account: str, server: str) -> None:
        index = self._read()
        for item in index["profiles"]:
            if (item.get("remote_id") == remote_id
                    and item.get("remote_account") == account
                    and item.get("remote_server") == server):
                for key in list(item):
                    if key.startswith("remote_") or key == "synced_hash":
                        del item[key]
        atomic_write_json(self.index_path, index)
