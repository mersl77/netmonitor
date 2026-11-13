"""Výběr synchronizovaných nastavení bez přenosu zařízení-specifických údajů."""
from __future__ import annotations

from .config import AppConfig

# Pouze nastavení profilů: žádná síťová konfigurace OS, souřadnice
# oken, přihlašovací údaje nebo historie měření.
_SHARED = ("ping", "notifications")


def export_shared(cfg: AppConfig) -> dict:
    data = cfg.normalized().to_dict()
    result = {key: data[key] for key in _SHARED}
    result["network"] = {"ip_profiles": data["network"]["ip_profiles"]}
    return result


def merge_shared(remote: dict, local: AppConfig) -> AppConfig:
    if not isinstance(remote, dict) or not isinstance(remote.get("ping"), dict):
        raise ValueError("Cloudový profil neobsahuje platné nastavení ping")
    # Zachová místní identitu síťové karty a umístěn…9656 tokens truncated…ekce (např. DNS, DOMÉNY, ZAŘÍZENÍ):")
        if ok and name.strip():
            sec = new_section(name)
            self._apply(lambda c: c.ping.targets.append(sec))
            self.table.select_id = sec.id
            self._fill_table()
            self.status.setText("Sekce přidána na konec – přetáhněte do ní cíle myší.")

    def _edit_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if not t:
            return
        if t.is_section:
            name, ok = QInputDialog.getText(self, "Přejmenovat sekci", "Název sekce:", text=t.name)
            if ok and name.strip():
                def fn(c: AppConfig):
                    x = c.target_by_id(tid)
                    if x:
                        x.name = name.strip()
                self._apply(fn)
                self._fill_table()
            return
        d = TargetDialog(t, self)
        if d.exec():
            nt = d.result_target()
            self._apply(lambda c: c.ping.targets.__setitem__(
                next(i for i, x in enumerate(c.ping.targets) if x.id == tid), nt))
            self._fill_table()

    def _del_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if not t:
            return
        if t.is_section:
            msg = f"Smazat sekci '{t.name}'?\nCíle v ní zůstanou zachované (připojí se k předchozí sekci)."
        else:
            msg = f"Smazat '{t.name}' ({t.host})?\nHistorie v databázi zůstane."
        if QMessageBox.question(self, "Smazat", msg) != QMessageBox.StandardButton.Yes:
            return
        self._apply(lambda c: setattr(c.ping, "targets", target_order.remove(c.ping.targets, tid)))
        self._fill_table()

    def _move(self, d: int) -> None:
        tid = self._selected_id()
        if not tid:
            return
        self._apply(lambda c: setattr(c.ping, "targets", target_order.move(c.ping.targets, tid, d)))
        self.table.select_id = tid
        self._fill_table()

    def _test_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if t and t.is_section:
            self.status.setText("Vyberte cíl, ne sekci.")
            return
        if tid and self.ctrl.engine.test_now(tid):
            self.status.setText("Test odeslán – výsledek se zobrazí ve widgetu.")
        elif tid:
            self.status.setText("Cíl je vypnutý – zapněte jej pro test.")

    def _graph_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if t and not t.is_section:
            self.history_requested.emit(tid)

    def _export(self) -> None:
        fn, _ = QFileDialog.getSaveFileName(self, "Export konfigurace",
                                            str(Path.home() / f"netmonitor-config-{time.strftime('%Y%m%d')}.json"),
                                            "JSON (*.json)")
        if fn:
            try:
                cfgmod.export_config(self.ctrl.cfg, Path(fn))
                self.status.setText(f"Exportováno: {fn}")
            except OSError as e:
                QMessageBox.warning(self, "Export", str(e))

    def _import(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(self, "Import konfigurace", str(Path.home()), "JSON (*.json)")
        if not fn:
            return
        try:
            cfg = cfgmod.import_config(Path(fn), self.ctrl.cfg)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import", f"Soubor nelze načíst:\n{e}")
            return
        cfg.appearance.x, cfg.appearance.y = self.ctrl.cfg.appearance.x, self.ctrl.cfg.appearance.y
        self.ctrl.update_config(cfg)
        self.load()
        self.status.setText(f"Importováno: {fn}")

    def _reset_targets(self) -> None:
        if QMessageBox.question(self, "Výchozí cíle", "Nahradit seznam cílů výchozími 10 položkami?") \
                == QMessageBox.StandardButton.Yes:
            self._apply(lambda c: setattr(c.ping, "targets", default_targets()))
            self._fill_table()

    # ------------------------------------------------------------------ Upozornění
    def _tab_notifications(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self._check("Povolit lokální upozornění", "notifications", "enabled"))
        for key, label in (("target_down", "Ztráta dostupnosti cíle"), ("target_up", "Obnovení dostupnosti cíle"),
                           ("wan_change", "Změna WAN IP"), ("internet_down", "Výpadek internetu"),
                           ("internet_up", "Obnovení internetu"), ("high_latency", "Dlouhodobě vysoká latence")):
            f.addRow(self._check(label, "notifications", key))
        f.addRow("Prodleva před upozorněním na výpadek", self._spin("notifications", "delay_s", 0, 3600, " s"))
        f.addRow("Min. odstup opakovaných upozornění", self._spin("notifications", "cooldown_s", 0, 86400, " s", 30))
        f.addRow("Vysoká latence nad", self._spin("notifications", "high_latency_ms", 1, 20000, " ms", 10, 0))
        f.addRow("… trvající alespoň", self._spin("notifications", "high_latency_duration_s", 10, 86400, " s", 10))
        f.addRow(hint("Výpadek se hlásí až po uplynutí prodlevy; krátké výpadky se tak nehlásí vůbec. "
                      "Obnovení se hlásí jen tehdy, pokud byl ohlášen výpadek."))
        return w

    # ------------------------------------------------------------------ Historie
    def _tab_history(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self._check("Ukládat historii měření", "history", "enabled"))
        f.addRow("Doba uchování", self._spin("history", "retention_days", 1, 3650, " dní"))
        b = QPushButton("Otevřít historii a grafy…")
        b.clicked.connect(lambda: self.history_requested.emit(""))
        f.addRow(b)
        f.addRow(hint(f"Databáze: {self.ctrl.history.path}\nMěření se agregují po minutách (min/průměr/max, "
                      "počty OK/chyb) a zapisují dávkově 1× za minutu – disk se zbytečně nezatěžuje. "
                      "Události výpadků se ukládají okamžitě a odděleně."))
        return w

    # ------------------------------------------------------------------ Profily
    def _tab_profiles(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(hint(
            "Vytvořte si profil Domácí, Pracovní nebo Zákazník. Nový profil vychází "
            "z aktuálního nastavení a ihned se aktivuje. Úpravy v ostatních kartách "
            "se ukládají do aktivního profilu. IP/DNS předvolby najdete v nastavení IP; "
            "změna profilu sama nemění síťovou konfiguraci operačního systému."
        ))
        self.profile_active = hint("")
        layout.addWidget(self.profile_active)
        self.profile_selector = QComboBox()
        layout.addWidget(self.profile_selector)
        row = QHBoxLayout()
        for title, callback in (
            ("Aktivovat / upravit", self._profile_activate),
            ("Nový profil", self._profile_create),
            ("Přejmenovat", self._profile_rename),
            ("Smazat", self._profile_delete),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        upload = QPushButton("Nahrát vybraný lokální profil na server")
        upload.clicked.connect(self._sync_upload)
        layout.addWidget(upload)
        layout.addWidget(hint("Cloudové profily — přihlášení na kartě Účet a synchronizace"))
        refresh = QPushButton("Načíst cloudové profily")
        refresh.clicked.connect(self._sync_refresh)
        layout.addWidget(refresh)
        self.sync_profile_list = QComboBox()
        layout.addWidget(self.sync_profile_list)
        row2 = QHBoxLayout()
        for name, handler in (("Stáhnout vybraný jako nový", self._sync_download),
                              ("Smazat z cloudu", self._sync_delete)):
            b = QPushButton(name)
            b.clicked.connect(handler)
            row2.addWidget(b)
        layout.addLayout(row2)
        self.sync_status = hint("Nepřihlášeno.")
        layout.addWidget(self.sync_status)
        layout.addStretch(1)
        self._profiles_refresh()
        return w

    def _profiles_refresh(self, selected=None) -> None:
        store = self.ctrl.profile_store
        self.profile_selector.blockSignals(True)
        self.profile_selector.clear()
        if store is not None:
            for p in store.list_profiles():
                self.profile_selector.addItem(p["name"], p["id"])
            idx = self.profile_selector.findData(selected or store.active_id())
            self.profile_active.setText("Aktivní profil: " + store.record(store.active_id())["name"])
            self.profile_selector.setCurrentIndex(idx)
        self.profile_selector.setEnabled(store is not None)
        self.profile_selector.blockSignals(False)

    def _profile_create(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            return
        name, ok = QInputDialog.getText(self, "Nový profil", "Název profilu:")
        if not ok:
            return
        try:
            pid = store.create(name, self.ctrl.cfg.copy())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Profily", str(exc))
            return
        self._profiles_refresh(pid)
        self._profile_activate()

    def _profile_rename(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            return
        pid = self.profile_selector.currentData()
        current = self.profile_selector.currentText()
        name, ok = QInputDialog.getText(self, "Přejmenovat profil", "Název:", text=current)
        if not ok:
            return
        try:
            linked = bool(store.record(pid).get("remote_id"))
            store.rename(pid, name)
            if linked and name.strip() != current:
                self.status.setText("Přejmenováno lokálně. Příští upload vytvoří novou cloudovou kopii; původní zůstane zachována.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Profily", str(exc))
            return
        self._profiles_refresh(pid)

    def _profile_activate(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            return
        pid = self.profile_selector.currentData()
        if pid == store.active_id():
            return
        try:
            # Zápis dokončíme ještě pod původním profilem.
            self.ctrl._save_timer.stop()
            self.ctrl._save_now()
            cfg = store.activate(pid)
            # Nesynchronizují se místní souřadnice okna.
            cfg.appearance.x = self.ctrl.cfg.appearance.x
            cfg.appearance.y = self.ctrl.cfg.appearance.y
            self.ctrl.update_config(cfg)
            self.ctrl._save_timer.stop()
            self.ctrl._save_now()
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Profily", str(exc))
            return
        self._profiles_refresh()
        self.load()
        self.status.setText(f"Aktivní profil: {self.profile_selector.currentText()}")

    def _profile_delete(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            return
        pid = self.profile_selector.currentData()
        if pid == store.active_id():
            QMessageBox.information(self, "Profily", "Nejprve aktivujte jiný profil.")
            return
        name = self.profile_selector.currentText()
        if QMessageBox.question(self, "Smazat profil", f"Opravdu odstranit profil '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        try:
            store.delete(pid)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Profily", str(exc))
            return
        self._profiles_refresh()

    # ------------------------------------------------------------------ Účet a synchronizace
    def _tab_account(self) -> QWidget:
        from ..core.sync_client import SyncClient
        from os import environ
        self.sync_client = SyncClient(environ.get("NETMONITOR_SYNC_URL", "https://sync.zelina.eu"))
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(hint("Přihlášení k vlastnímu synchronizačnímu serveru. "
                              "První účet musí nejprve bezpečně založit správce serveru; "
                              "veřejná registrace je vypnutá. Žádný výchozí účet ani heslo neexistuje. "
                              "Heslo a přístupový token zůstávají pouze v paměti aplikace. "
                              "Zatím jde o ruční přenos profilů, nikoli automatickou synchronizaci."))
        form = QFormLayout()
        self.sync_email = QLineEdit()
        self.sync_email.setPlaceholderText("uzivatel@domena.cz")
        self.sync_password = QLineEdit()
        self.sync_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("E-mail", self.sync_email)
        form.addRow("Heslo", self.sync_password)
        layout.addLayout(form)
        row = QHBoxLayout()
        for name, handler in (("Přihlásit", self._sync_login),
                              ("Odhlásit", self._sync_logout),
                              ("Načíst cloudové profily", self._sync_refresh)):
            b = QPushButton(name)
            b.clicked.connect(handler)
            row.addWidget(b)
        layout.addLayout(row)
        self.account_status = hint("Nepřihlášeno. Profily spravujte na kartě Profily.")
        layout.addWidget(self.account_status)
        layout.addStretch(1)
        return w

    def _sync_message(self, message: str) -> None:
        self.sync_status.setText(message)
        self.account_status.setText(message)

    def _sync_job(self, key: str, func: Callable[[], object]) -> None:
        if self._sync_busy:
            return
        self._sync_busy = True
        self.tabs.setEnabled(False)
        # Veškeré síťové požadavky běží mimo vlákno GUI.
        self._sync_message("Komunikuji se serverem…")

        def wrapped():
            try:
                return (True, func())
            except Exception as exc:
                return (False, str(exc))

        self.ctrl._run_bg("sync-" + key, wrapped,
                          lambda result: self.sync_finished.emit(key, result))

    def _sync_done(self, key: str, result: object) -> None:
        self._sync_busy = False
        self.tabs.setEnabled(True)
        ok, value = result
        if not ok:
            self._sync_message("Chyba: " + str(value))
            return
        if key == "login":
            self._sync_account = value
            self.sync_email.setReadOnly(True)
            self.sync_password.clear()
            self.tabs.setCurrentIndex(6)
            self._sync_message("Přihlášeno. Přístupový token platí omezenou dobu.")
            self._sync_refresh()
        elif key == "refresh":
            self.sync_profile_list.clear()
            for p in value:
                if isinstance(p, dict) and p.get("id") and p.get("name"):
                    self.sync_profile_list.addItem(p["name"], p)
            self._sync_message("Cloudové profily načteny." if self.sync_profile_list.count()
                                     else "Cloud je prázdný. Vyberte lokální profil a nahrajte jej na server.")
        elif key == "upload":
            local_id, remote_id, revision, account, server = value
            try:
                self.ctrl.profile_store.bind_remote(local_id, remote_id, revision, account, server)
            except (OSError, ValueError) as exc:
                self._sync_message("Cloud uložen, ale lokální vazba selhala: " + str(exc))
                return
            self._sync_message("Profil uložen na server. Načítám seznam…")
            self._sync_refresh()
        elif key == "delete":
            try:
                self.ctrl.profile_store.unbind_remote(*value)
            except (OSError, ValueError) as exc:
                self._sync_message("Cloud smazán, lokální vazbu nelze odstranit: " + str(exc))
                return
            self._sync_message("Cloudový profil smazán. Lokální profily zůstávají zachovány.")
            self._sync_refresh()
        elif key == "download":
            try:
                from ..core.sync_config import merge_shared
                remote, base, account, server = value
                from uuid import UUID
                UUID(remote["id"])
                if not isinstance(remote.get("revision"), int) or remote["revision"] < 1:
                    raise ValueError("Neplatná revize profilu")
                config = merge_shared(remote["config"], base)
                store = self.ctrl.profile_store
                existing = {p["name"].casefold() for p in store.list_profiles()}
                name = str(remote["name"]).strip()[:100] or "Cloudový profil"
                candidate, i = name, 2
                while candidate.casefold() in existing:
                    suffix = f" ({i})"
                    candidate = name[:100-len(suffix)] + suffix
                    i += 1
                local_id = store.create(candidate, config)
                store.bind_remote(local_id, remote["id"], remote["revision"], account, server)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._sync_message("Stažení nelze uložit: " + str(exc))
                return
            self._sync_message("Profil stažen a uložen lokálně; aktivujte jej na kartě Profily.")
            self._profiles_refresh(local_id)

    def _sync_login(self) -> None:
        email = self.sync_email.text().strip()
        password = self.sync_password.text()
        if not email or not password:
            self._sync_message("Vyplňte e-mail a heslo.")
            return
        def login():
            self.sync_client.login(email, password)
            return email.casefold()
        self._sync_job("login", login)

    def _sync_logout(self) -> None:
        self.sync_client.logout()
        self._sync_account = ""
        self.sync_email.setReadOnly(False)
        self.sync_password.clear()
        self.sync_profile_list.clear()
        self._sync_message("Odhlášeno.")

    def _sync_refresh(self) -> None:
        self._sync_job("refresh", self.sync_client.profiles)

    def _sync_upload(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            self._sync_message("Lokální profily nejsou dostupné.")
            return
        self.ctrl._save_timer.stop()
        try:
            self.ctrl._save_now()
        except OSError as exc:
            self._sync_message("Konfiguraci nelze uložit: " + str(exc))
            return
        local_id = self.profile_selector.currentData()
        record = store.record(local_id)
        name = record["name"]
        from ..core.sync_config import export_shared
        config = export_shared(store.load(local_id))
        account = self._sync_account
        server = self.sync_client.base_url

        def upload():
            if (record.get("remote_id") and record.get("remote_account") == account
                    and record.get("remote_server") == server):
                result = self.sync_client.upload(
                    record["remote_id"], config, record["remote_revision"]
                )
            else:
                result = self.sync_client.create(name, config)
            return (local_id, result["id"], result["revision"], account, server)

        self._sync_job("upload", upload)

    def _sync_download(self) -> None:
        store = self.ctrl.profile_store
        if store is None:
            return
        item = self.sync_profile_list.currentData()
        if not isinstance(item, dict):
            self._sync_message("Vyberte cloudový profil.")
            return
        profile_id = item["id"]
        account = self._sync_account
        server = self.sync_client.base_url

        base = self.ctrl.cfg.copy()

        def download():
            result = self.sync_client.download(profile_id)
            return result, base, account, server

        self._sync_job("download", download)

    def _sync_delete(self) -> None:
        item = self.sync_profile_list.currentData()
        if not isinstance(item, dict):
            self._sync_message("Vyberte cloudový profil.")
            return
        if QMessageBox.question(self, "Smazat z cloudu",
                                f"Smazat '{item['name']}' ze serveru? Lokální kopie zůstanou.") != QMessageBox.StandardButton.Yes:
            return
        account, server = self._sync_account, self.sync_client.base_url
        def delete():
            self.sync_client.delete(item["id"])
            return item["id"], account, server
        self._sync_job("delete", delete)
