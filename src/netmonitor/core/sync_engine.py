"""Automatická synchronizace profilů se serverem.

Rozdělení kvůli vláknům: `snapshot()` a `apply()` pracují s místním úložištěm (vlákno GUI),
`run()` jen se sítí (vlákno na pozadí). Nic se nepřepisuje potichu: když se profil změnil
na tomto počítači i na serveru, vznikne konflikt a rozhodne uživatel.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .config import AppConfig
from .profiles import ProfileStore
from .sync_client import SyncClient, SyncError
from .sync_config import export_shared, merge_shared

KEEP_LOCAL, KEEP_REMOTE, KEEP_BOTH = "local", "remote", "both"


def profile_hash(name: str, shared: dict) -> str:
    raw = json.dumps({"name": name, "config": shared}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class LocalProfile:
    id: str
    name: str
    cfg: AppConfig
    hash: str
    remote_id: str | None = None
    remote_revision: int = 0
    synced_hash: str = ""

    @property
    def dirty(self) -> bool:
        return self.hash != self.synced_hash


@dataclass
class Snapshot:
    account: str
    server: str
    profiles: list[LocalProfile]
    deletes: list[str] = field(default_factory=list)


@dataclass
class Action:
    kind: str                 # bound | downloaded | new | fork | gone | conflict | deleted | error
    local_id: str | None = None
    remote: dict | None = None        # úplný vzdálený profil (downloaded/new/fork) nebo souhrn (conflict)
    revision: int = 0
    hash: str = ""                    # otisk místního obsahu, ze kterého akce vycházela
    message: str = ""
    name: str = ""


def snapshot(store: ProfileStore, account: str, server: str) -> Snapshot:
    account, server = account.strip().casefold(), server.rstrip("/")
    profiles = []
    for rec in store.list_profiles():
        if rec.get("local_only"):
            continue
        bound = bool(rec.get("remote_id"))
        if bound and (rec.get("remote_account"), rec.get("remote_server")) != (account, server):
            continue                                   # patří k jinému účtu – nikdy nenahrávat jinam
        cfg = store.load(rec["id"])
        profiles.append(LocalProfile(
            id=rec["id"], name=rec["name"], cfg=cfg, hash=profile_hash(rec["name"], export_shared(cfg)),
            remote_id=rec.get("remote_id") if bound else None,
            remote_revision=int(rec.get("remote_revision", 0)) if bound else 0,
            synced_hash=rec.get("synced_hash", "") if bound else ""))
    return Snapshot(account, server, profiles, store.pending_deletes(account, server))


def run(client: SyncClient, snap: Snapshot, resolutions: dict[str, str] | None = None) -> list[Action]:
    """Síťová část. Chyba spojení přeruší celý běh (SyncError), chyba jednoho profilu jen ten profil."""
    resolutions = resolutions or {}
    actions: list[Action] = []
    for remote_id in snap.deletes:
        try:
            client.delete(remote_id)
        except SyncError as exc:
            if exc.status != 404:
                if exc.status is None or exc.status == 401:
                    raise
                actions.append(Action("error", message=str(exc)))
                continue
        actions.append(Action("deleted", remote={"id": remote_id}))
    remote = {str(p.get("id")): p for p in client.profiles()}
    claimed = {p.remote_id for p in snap.profiles if p.remote_id}
    by_name = {str(p.get("name", "")).casefold(): p for rid, p in remote.items() if rid not in claimed}

    def step(lp: LocalProfile) -> Action:
        if lp.remote_id:
            summary = remote.get(lp.remote_id)
            if summary is None:
                return Action("gone", lp.id, name=lp.name)
            if summary.get("revision") == lp.remote_revision:
                return upload(lp, lp.remote_id, lp.remote_revision) if lp.dirty else Action("noop", lp.id)
            if not lp.dirty:
                return Action("downloaded", lp.id, client.download(lp.remote_id), hash=lp.hash)
            return resolve(lp, summary, None)
        summary = by_name.pop(lp.name.casefold(), None)
        if summary is None:
            created = client.create(lp.name, export_shared(lp.cfg))
            return Action("bound", lp.id, created, created["revision"], lp.hash)
        # Stejně pojmenovaný profil už na serveru je (typicky první přihlášení na dalším počítači).
        claimed.add(str(summary["id"]))
        full = client.download(str(summary["id"]))
        same = profile_hash(str(full.get("name", "")), export_shared(merge_shared(full["config"], lp.cfg)))
        if same == lp.hash:
            return Action("bound", lp.id, full, full["revision"], lp.hash)
        return resolve(lp, summary, full)

    def upload(lp: LocalProfile, remote_id: str, revision: int) -> Action:
        result = client.upload(remote_id, export_shared(lp.cfg), revision, lp.name)
        return Action("bound", lp.id, result, result["revision"], lp.hash)

    def resolve(lp: LocalProfile, summary: dict, full: dict | None) -> Action:
        choice = resolutions.get(lp.id)
        rid = str(summary["id"])
        if choice == KEEP_LOCAL:
            return upload(lp, rid, int(summary["revision"]))
        if choice in (KEEP_REMOTE, KEEP_BOTH):
            full = full or client.download(rid)
            return Action("downloaded" if choice == KEEP_REMOTE else "fork", lp.id, full, hash=lp.hash)
        return Action("conflict", lp.id, summary, name=lp.name)

    for lp in snap.profiles:
        try:
            actions.append(step(lp))
        except SyncError as exc:
            if exc.status is None or exc.status == 401:
                raise
            if exc.code == "revision_conflict":
                actions.append(Action("conflict", lp.id, remote.get(lp.remote_id or "", {}), name=lp.name))
            else:
                actions.append(Action("error", lp.id, message=str(exc), name=lp.name))
        except (ValueError, KeyError, TypeError) as exc:
            actions.append(Action("error", lp.id, message=f"Neplatná data ze serveru: {exc}", name=lp.name))
    for summary in by_name.values():
        try:
            actions.append(Action("new", remote=client.download(str(summary["id"]))))
        except SyncError as exc:
            if exc.status is None or exc.status == 401:
                raise
            actions.append(Action("error", message=str(exc), name=str(summary.get("name", ""))))
    return [a for a in actions if a.kind != "noop"]


@dataclass
class Outcome:
    changed: bool = False                    # změnil se seznam nebo obsah místních profilů
    active_cfg: AppConfig | None = None      # nový obsah aktivního profilu (má se hned použít)
    conflicts: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    uploaded: int = 0
    downloaded: int = 0


def apply(store: ProfileStore, snap: Snapshot, actions: list[Action], base: AppConfig) -> Outcome:
    """Zapíše výsledky do místního úložiště. Profil změněný během běhu se nepřepíše."""
    out = Outcome()
    records = {p["id"]: p for p in store.list_profiles()}

    def unchanged(action: Action) -> bool:
        rec = records.get(action.local_id)
        if rec is None:
            return False
        return profile_hash(rec["name"], export_shared(store.load(rec["id"]))) == action.hash

    def take(local_id: str, full: dict) -> None:
        cfg = merge_shared(full["config"], store.load(local_id))
        store.save(local_id, cfg)
        wanted = str(full.get("name", "")).strip()[:100]
        if wanted and wanted != records[local_id]["name"] and store.unique_name(wanted, local_id) == wanted:
            store.rename(local_id, wanted)
        # Otisk se počítá se jménem ze serveru: když místní přejmenování neprošlo, profil zůstane „změněný“.
        store.bind_remote(local_id, str(full["id"]), int(full["revision"]), snap.account, snap.server,
                          profile_hash(wanted, export_shared(cfg)))
        if local_id == store.active_id():
            out.active_cfg = cfg
        out.changed = True
        out.downloaded += 1

    for a in actions:
        try:
            if a.kind == "deleted":
                store.clear_pending_delete(a.remote["id"])
            elif a.kind == "bound":
                if a.local_id in records:
                    store.bind_remote(a.local_id, str(a.remote["id"]), a.revision, snap.account, snap.server, a.hash)
                    out.uploaded += 1
            elif a.kind == "downloaded":
                if unchanged(a):
                    take(a.local_id, a.remote)
            elif a.kind == "fork":
                if unchanged(a):
                    rec = records[a.local_id]
                    store.rename(a.local_id, store.unique_name(rec["name"] + " (místní kopie)", a.local_id))
                    store.unbind(a.local_id)
                    new_id = store.create(store.unique_name(str(a.remote["name"])), merge_shared(a.remote["config"], base))
                    records = {p["id"]: p for p in store.list_profiles()}
                    take(new_id, a.remote)
            elif a.kind == "new":
                new_id = store.create(store.unique_name(str(a.remote["name"])), merge_shared(a.remote["config"], base))
                records = {p["id"]: p for p in store.list_profiles()}
                take(new_id, a.remote)
            elif a.kind == "gone":
                if a.local_id in records:
                    store.unbind(a.local_id, local_only=True)
                    out.changed = True
            elif a.kind == "conflict":
                out.conflicts.append(a)
            elif a.kind == "error":
                out.errors.append(f"{a.name}: {a.message}" if a.name else a.message)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            out.errors.append(f"{a.name or 'Profil'}: {exc}")
    return out
