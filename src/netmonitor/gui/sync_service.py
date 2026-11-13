"""Účet a automatická synchronizace profilů – propojení klienta, úložiště a GUI.

Síť běží vždy na pozadí (Controller._run_bg); místní soubory se čtou a zapisují jen ve vlákně GUI.
Bez přihlášení se nic neodesílá a aplikace funguje čistě lokálně.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ..core import sync_engine
from ..core.config import atomic_write_json
from ..core.secret_store import SecretStore
from ..core.sync_client import DEFAULT_SERVER, SyncClient, SyncError
from ..utils.paths import config_dir

log = logging.getLogger(__name__)
SETTINGS_FILE = "sync.json"


def _safe(fn: Callable[[], object]) -> Callable[[], object]:
    """Očekávané chyby serveru se vracejí jako hodnota (bez výpisu zásobníku do logu)."""
    def call():
        try:
            return fn()
        except SyncError as exc:
            return exc
    return call


class SyncService(QObject):
    changed = Signal()                  # stav přihlášení / text stavu
    conflicts_found = Signal(object)    # list[sync_engine.Action]

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.secrets = SecretStore()
        self._settings_path = config_dir() / SETTINGS_FILE
        self.settings = self._load_settings()
        self.client = self._make_client(self.settings["server"])
        self.status = "Nepřihlášeno – profily jsou uložené jen v tomto počítači."
        self.last_sync: float | None = None
        self.busy = False
        self.remember = True
        self.errors: list[str] = []
        self.pending_conflicts: list = []
        self._again = False
        self._resolutions: dict[str, str] = {}
        self._asked: set = set()            # konflikty, na které už byl uživatel dotázán
        self._debounce = QTimer(self, singleShot=True, interval=4000, timeout=self.sync_now)
        self._periodic = QTimer(self, interval=5 * 60 * 1000, timeout=self.sync_now)
        ctrl.config_changed.connect(self._local_change)
        ctrl.profiles_changed.connect(self._local_change)

    # ------------------------------------------------------------------ nastavení
    def _load_settings(self) -> dict:
        data = {"server": os.environ.get("NETMONITOR_SYNC_URL", DEFAULT_SERVER), "email": ""}
        try:
            with self._settings_path.open(encoding="utf-8") as f:
                stored = json.load(f)
            for key in data:
                if isinstance(stored.get(key), str) and stored[key]:
                    data[key] = stored[key]
        except (OSError, ValueError, AttributeError):
            pass
        return data

    def _save_settings(self) -> None:
        try:
            atomic_write_json(self._settings_path, self.settings)
        except OSError as exc:
            log.warning("Nastavení synchronizace nelze uložit: %s", exc)

    def _make_client(self, server: str) -> SyncClient:
        try:
            client = SyncClient(server)
        except ValueError:
            client = SyncClient(DEFAULT_SERVER)
            self.settings["server"] = DEFAULT_SERVER
        client.on_tokens = self._tokens_changed
        return client

    def set_server(self, server: str) -> None:
        """Změna serveru odhlásí. Vyhazuje ValueError u neplatné adresy."""
        server = server.strip().rstrip("/")
        if server == self.client.base_url:
            return
        SyncClient(server)                       # ověření adresy
        if self.client.logged_in:
            self.logout()
        self.settings["server"] = server
        self._save_settings()
        self.client = self._make_client(server)

    def _tokens_changed(self, refresh: str | None) -> None:
        # Volá se z vlákna na pozadí; zápis malého souboru je bezpečný a musí proběhnout hned,
        # jinak by po pádu aplikace zůstal uložený už vyměněný (neplatný) token.
        try:
            if refresh is None:
                self.secrets.clear()
            elif self.remember:
                self.secrets.save(self.client.base_url, self.client.email or self.settings["email"], refresh)
        except OSError as exc:
            log.warning("Přihlášení nelze uložit: %s", exc)

    # ------------------------------------------------------------------ stav
    @property
    def logged_in(self) -> bool:
        return self.client.logged_in and self.client.user is not None

    @property
    def account(self) -> str:
        return self.client.email.casefold()

    def _set_status(self, text: str) -> None:
        self.status = text
        self.changed.emit()

    def profile_state(self, record: dict) -> str:
        """Krátký popis stavu synchronizace jednoho místního profilu."""
        if not self.logged_in:
            return "jen v tomto počítači" if not record.get("remote_id") else "uloženo na serveru (nepřihlášeno)"
        if record.get("local_only"):
            return "jen místní (na serveru smazán)"
        if record.get("remote_id"):
            if (record.get("remote_account"), record.get("remote_server")) != (self.account, self.client.base_url):
                return "patří k jinému účtu"
            if any(a.local_id == record["id"] for a in self.pending_conflicts):
                return "konflikt – čeká na rozhodnutí"
            return "synchronizováno" if self._clean(record) else "čeká na nahrání"
        return "čeká na nahrání"

    def _clean(self, record: dict) -> bool:
        store = self.ctrl.profile_store
        try:
            from ..core.sync_config import export_shared
            # Aktivní profil: rozhoduje běžící konfigurace (na disk se zapisuje s malým zpožděním).
            cfg = self.ctrl.cfg if record["id"] == store.active_id() else store.load(record["id"])
            return sync_engine.profile_hash(record["name"], export_shared(cfg)) == record.get("synced_hash")
        except (OSError, ValueError, KeyError):
            return False

    # ------------------------------------------------------------------ účet
    def _job(self, key: str, fn: Callable[[], object], done: Callable[[object], None]) -> bool:
        if self.busy:
            return False
        self.busy = True
        self._set_status("Komunikuji se serverem…")

        def finished(result):
            self.busy = False
            done(result)

        self.ctrl._run_bg("sync-" + key, _safe(fn), finished)
        return True

    def start(self) -> None:
        """Po startu aplikace obnoví uložené přihlášení (pokud existuje)."""
        stored = self.secrets.load()
        if stored is None or stored[0] != self.client.base_url:
            return
        _server, email, token = stored
        self.settings["email"] = email

        def done(result):
            if isinstance(result, Exception):
                if self.client.refresh_token:       # jen výpadek spojení – zkusíme později
                    self._set_status(f"Server není dostupný, pracuji offline. ({result})")
                    self._periodic.start()
                else:
                    self._set_status("Přihlášení vypršelo. Přihlaste se znovu v Nastavení → Účet.")
                return
            self._logged_in()

        self._job("resume", lambda: self.client.resume(token), done)

    def login(self, email: str, password: str, code: str | None, remember: bool,
              done: Callable[[SyncError | None], None]) -> None:
        self._auth("login", lambda: self.client.login(email, password, code), email, remember, done)

    def register(self, email: str, password: str, license_key: str, remember: bool,
                 done: Callable[[SyncError | None], None]) -> None:
        self._auth("register", lambda: self.client.register(email, password, license_key), email, remember, done)

    def _auth(self, key, fn, email, remember, done) -> None:
        self.remember = remember
        self.settings["email"] = email.strip()

        def finished(result):
            if isinstance(result, Exception):
                error = result if isinstance(result, SyncError) else SyncError(str(result))
                self._set_status("Nepřihlášeno: " + str(error) if error.code != "totp_required"
                                 else "Zadejte kód z autentikátoru.")
                done(error)
                return
            self._save_settings()
            self._logged_in()
            done(None)

        if not self._job(key, fn, finished):
            done(SyncError("Probíhá jiná operace, zkuste to za okamžik."))

    def _logged_in(self) -> None:
        self._periodic.start()
        self._set_status(f"Přihlášeno jako {self.client.email}.")
        self.sync_now()

    def logout(self) -> None:
        self._periodic.stop()
        self._debounce.stop()
        client = self.client
        self.pending_conflicts, self.errors = [], []
        self._asked.clear()
        if client.access_token and not self.ctrl._stopping:
            # Relaci na serveru ukončí samostatný klient; ten hlavní se vyprázdní hned.
            old = SyncClient(client.base_url, access_token=client.access_token)
            self.ctrl._run_bg("sync-logout", old.logout, lambda _r: None)
        client.access_token = client.refresh_token = client.user = None
        client.mfa = False
        self.secrets.clear()
        self._set_status("Odhlášeno – profily zůstávají v tomto počítači.")

    def run(self, key: str, fn: Callable[[], object], done: Callable[[object], None]) -> None:
        """Obecný požadavek na server na pozadí (správa klíčů, 2FA…). done dostane výsledek nebo výjimku."""
        self.ctrl._run_bg("sync-x-" + key, _safe(fn), done)

    # ------------------------------------------------------------------ synchronizace
    def _local_change(self, *_args) -> None:
        if self.logged_in and self.ctrl.profile_store is not None:
            self._debounce.start()
            self.changed.emit()

    def resolve(self, local_id: str, choice: str) -> None:
        self._resolutions[local_id] = choice
        self.pending_conflicts = [a for a in self.pending_conflicts if a.local_id != local_id]

    def sync_now(self) -> None:
        store = self.ctrl.profile_store
        if store is None or not self.logged_in or self.ctrl._stopping:
            return
        if self.busy:
            self._again = True
            return
        try:
            if self.ctrl._save_timer.isActive():
                self.ctrl._save_timer.stop()
                self.ctrl._save_now()
            snap = sync_engine.snapshot(store, self.account, self.client.base_url)
        except (OSError, ValueError, KeyError) as exc:
            self._set_status(f"Synchronizace selhala: {exc}")
            return
        resolutions, self._resolutions = self._resolutions, {}
        client = self.client
        self._job("run", lambda: sync_engine.run(client, snap, resolutions),
                  lambda result: self._finished(snap, result))

    def _finished(self, snap, result) -> None:
        store = self.ctrl.profile_store
        if isinstance(result, SyncError) and result.status == 401 and not self.client.logged_in:
            self._periodic.stop()
            self._set_status("Přihlášení vypršelo nebo bylo zrušeno. Přihlaste se znovu.")
            return
        if isinstance(result, Exception):
            self._set_status(f"Synchronizace odložena: {result}")
            return
        if not self.logged_in or snap.account != self.account:
            self.changed.emit()                  # mezitím odhlášeno – výsledek se zahodí
            return
        try:
            out = sync_engine.apply(store, snap, result, self.ctrl.cfg.copy())
        except (OSError, ValueError, KeyError) as exc:
            self._set_status(f"Synchronizaci nelze uložit: {exc}")
            return
        self.errors = out.errors
        self.pending_conflicts = out.conflicts
        self.last_sync = time.time()
        if out.active_cfg is not None:
            self.ctrl.apply_profile_config(out.active_cfg)
        if out.changed:
            self.ctrl.profiles_changed.emit()
        parts = [f"Synchronizováno {time.strftime('%H:%M:%S')}"]
        if out.uploaded:
            parts.append(f"nahráno {out.uploaded}")
        if out.downloaded:
            parts.append(f"staženo {out.downloaded}")
        if out.conflicts:
            parts.append(f"konflikty: {len(out.conflicts)}")
        if out.errors:
            parts.append("chyby: " + "; ".join(out.errors[:3]))
        self._set_status(f"Přihlášeno jako {self.client.email}. " + ", ".join(parts) + ".")
        fresh = [a for a in out.conflicts if (a.local_id, (a.remote or {}).get("revision")) not in self._asked]
        self._asked.update((a.local_id, (a.remote or {}).get("revision")) for a in fresh)
        if fresh:
            self.conflicts_found.emit(fresh)
        if self._again or (out.changed and any(a.kind == "fork" for a in result)):
            self._again = False
            QTimer.singleShot(0, self.sync_now)
