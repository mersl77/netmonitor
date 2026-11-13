"""Karty Nastavení: Profily, Účet a synchronizace, Správa (licenční klíče – jen správce serveru)."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import __version__
from ..core import sync_engine
from ..core.sync_client import SyncError

DOWNLOAD_URL = f"https://github.com/mersl77/netmonitor/releases/tag/v{__version__}"

KEY_STATES = {"active": "platný", "used": "využitý", "expired": "vypršel", "revoked": "zneplatněn"}


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("hint", True)
    return label


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill(t: QTableWidget, rows: list[tuple[object, list[str]]]) -> None:
    """Naplní tabulku; první sloupec nese data řádku a výběr se zachová."""
    current = t.item(t.currentRow(), 0).data(Qt.ItemDataRole.UserRole) if t.currentRow() >= 0 else None
    t.setRowCount(len(rows))
    select = 0
    for r, (data, cells) in enumerate(rows):
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if c == 0:
                item.setData(Qt.ItemDataRole.UserRole, data)
            t.setItem(r, c, item)
        if current is not None and _row_id(data) == _row_id(current):
            select = r
    if rows:
        t.setCurrentCell(select, 0)


def _row_id(data) -> object:
    return data.get("id") if isinstance(data, dict) else data


def selected(t: QTableWidget):
    item = t.item(t.currentRow(), 0) if t.currentRow() >= 0 else None
    return item.data(Qt.ItemDataRole.UserRole) if item else None


def when(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def qr_pixmap(text: str, size: int = 232) -> QPixmap:
    """QR kód černě na bílém s okrajem 4 moduly (nutné pro spolehlivé načtení telefonem)."""
    from ..core.qr import matrix
    modules = matrix(text)
    count = len(modules) + 8
    scale = max(2, size // count)
    image = QImage(count * scale, count * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    black = QColor("black")
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect((x + 4) * scale, (y + 4) * scale, scale, scale, black)
    painter.end()
    return QPixmap.fromImage(image)


def invitation(key: dict, server: str) -> str:
    """Text pro kamaráda: odkaz ke stažení, postup registrace a klíč."""
    uses = int(key.get("max_uses", 1))
    limits = "pro jeden účet" if uses == 1 else f"pro {uses} účtů"
    if key.get("expires_at"):
        limits += f" a je potřeba ho použít do {when(key['expires_at'])}"
    return (
        "Ahoj, posílám přístup do NetMonitoru (sledování sítě a ping cílů se synchronizací profilů).\n\n"
        f"1. Stáhni a nainstaluj: {DOWNLOAD_URL}\n"
        "2. Pravé tlačítko na widgetu → Nastavení → karta Účet a synchronizace.\n"
        f"3. Server: {server}\n"
        "4. Vyplň svůj e-mail a nové heslo (alespoň 10 znaků).\n"
        f"5. Licenční klíč: {key.get('key', '')}\n"
        "6. Klikni na „Registrovat nový účet“.\n\n"
        f"Klíč platí {limits}."
    )


def ask_conflicts(sync, conflicts, parent=None) -> None:
    """Zeptá se na každý konflikt; nic se nepřepíše bez rozhodnutí uživatele."""
    decided = False
    for action in list(conflicts):
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("NetMonitor – konflikt profilu")
        box.setText(f"Profil „{action.name}“ se změnil na tomto počítači i na serveru.")
        box.setInformativeText(f"Verze na serveru je z {when((action.remote or {}).get('updated_at'))}. "
                               "Kterou verzi chcete zachovat?")
        local = box.addButton("Nahrát verzi z tohoto počítače", QMessageBox.ButtonRole.AcceptRole)
        remote = box.addButton("Použít verzi ze serveru", QMessageBox.ButtonRole.DestructiveRole)
        both = box.addButton("Ponechat obě", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Rozhodnu později", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        choice = next((value for button, value in ((local, sync_engine.KEEP_LOCAL),
                                                   (remote, sync_engine.KEEP_REMOTE),
                                                   (both, sync_engine.KEEP_BOTH)) if clicked == button), None)
        if choice:
            sync.resolve(action.local_id, choice)
            decided = True
    if decided:
        sync.sync_now()


# ====================================================================== Profily
class ProfilesTab(QWidget):
    switched = Signal()

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "Profil je samostatná sada ping cílů (IP adresy, domény, sekce), upozornění a uložených IP/DNS "
            "předvoleb – například Soukromý, Firemní nebo Zákazník. Přepnout jde tady i pravým tlačítkem "
            "na widgetu nebo ikoně v liště (Profil). Úpravy v ostatních kartách se ukládají do aktivního profilu. "
            "Přepnutí samo nemění IP adresu počítače; může ale nabídnout uloženou předvolbu (Automatika)."))
        self.table = table(["Profil", "Síť", "Stav"])
        self.table.itemDoubleClicked.connect(lambda _i: self._activate())
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        for title, callback in (("Přepnout na vybraný", self._activate), ("Nový profil", self._create),
                                ("Přejmenovat", self._rename), ("Smazat", self._delete)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        row2 = QHBoxLayout()
        self.btn_sync = QPushButton("Synchronizovat nyní")
        self.btn_sync.clicked.connect(self._sync)
        self.btn_conflicts = QPushButton("Vyřešit konflikty…")
        self.btn_conflicts.clicked.connect(self._conflicts)
        self.btn_reupload = QPushButton("Nahrát vybraný do tohoto účtu")
        self.btn_reupload.setToolTip("Profil, který byl na serveru smazán nebo patří jinému účtu, se sám nenahrává. "
                                     "Tímto jej nahrajete do právě přihlášeného účtu.")
        self.btn_reupload.clicked.connect(self._reupload)
        for button in (self.btn_sync, self.btn_conflicts, self.btn_reupload):
            row2.addWidget(button)
        layout.addLayout(row2)
        box = QGroupBox("Automatika")
        form = QFormLayout(box)
        self.ip_preset = QComboBox()
        self.ip_preset.setToolTip("Po přepnutí na aktivní profil se aplikace zeptá, zda tuto IP/DNS předvolbu použít. "
                                  "Předvolby se ukládají v dialogu Nastavit IP adresu.")
        self.ip_preset.activated.connect(self._set_ip_preset)
        form.addRow("IP/DNS předvolba aktivního profilu", self.ip_preset)
        net_row = QHBoxLayout()
        self.btn_bind = QPushButton("Spojit vybraný profil s touto sítí")
        self.btn_bind.setToolTip("Po připojení k této síti aplikace nabídne přepnutí na vybraný profil. "
                                 "Platí jen pro tento počítač.")
        self.btn_bind.clicked.connect(self._bind_network)
        self.btn_unbind = QPushButton("Zrušit spojení se sítí")
        self.btn_unbind.clicked.connect(self._unbind_network)
        net_row.addWidget(self.btn_bind)
        net_row.addWidget(self.btn_unbind)
        self.net_label = hint("")
        form.addRow("Aktuální síť", self.net_label)
        form.addRow("", net_row)
        layout.addWidget(box)
        self.status = hint("")
        layout.addWidget(self.status)
        ctrl.profiles_changed.connect(self.refresh)
        ctrl.network_identified.connect(self._buttons)
        ctrl.config_changed.connect(lambda _c: self._fill_ip_presets())
        if ctrl.sync is not None:
            ctrl.sync.changed.connect(self.refresh)
        self.table.currentCellChanged.connect(lambda *_: self._buttons())
        self.refresh()

    def refresh(self, select: str | None = None) -> None:
        store, sync = self.ctrl.profile_store, self.ctrl.sync
        self.setEnabled(store is not None)
        if store is None:
            self.status.setText("Profily nejsou v tomto režimu dostupné.")
            return
        try:
            active = store.active_id()
            records = store.list_profiles()
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(f"Profily nelze načíst: {exc}")
            return
        rows = []
        for rec in records:
            state = sync.profile_state(rec) if sync is not None else "jen v tomto počítači"
            nets = ", ".join(n.get("label", "") for n in rec.get("networks", [])) or "—"
            rows.append((rec["id"], [rec["name"] + ("   ● aktivní" if rec["id"] == active else ""), nets, state]))
        fill(self.table, rows)
        bold = QFont(self.table.font())
        bold.setBold(True)
        for r, rec in enumerate(records):
            if rec["id"] == active:
                self.table.item(r, 0).setFont(bold)
            if rec["id"] == select:
                self.table.setCurrentCell(r, 0)
        self.status.setText(sync.status if sync is not None else "")
        self._fill_ip_presets()
        self._buttons()

    def _fill_ip_presets(self) -> None:
        net = self.ctrl.cfg.network
        self.ip_preset.blockSignals(True)
        self.ip_preset.clear()
        self.ip_preset.addItem("žádná – na nic se neptat", "")
        for p in net.ip_profiles:
            self.ip_preset.addItem(p["name"], p["name"])
        self.ip_preset.setCurrentIndex(max(0, self.ip_preset.findData(net.switch_ip_profile)))
        self.ip_preset.blockSignals(False)

    def _set_ip_preset(self, _index: int) -> None:
        cfg = self.ctrl.cfg.copy()
        cfg.network.switch_ip_profile = self.ip_preset.currentData() or ""
        self.ctrl.update_config(cfg)

    def _bind_network(self) -> None:
        store, pid, identity = self.ctrl.profile_store, selected(self.table), self.ctrl.net_identity
        if not pid or identity is None:
            return
        try:
            store.bind_network(pid, identity[0], identity[1])
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.refresh(pid)

    def _unbind_network(self) -> None:
        store, pid = self.ctrl.profile_store, selected(self.table)
        if not pid:
            return
        try:
            store.unbind_networks(pid)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.refresh(pid)

    def _buttons(self) -> None:
        sync, store = self.ctrl.sync, self.ctrl.profile_store
        identity = self.ctrl.net_identity
        self.net_label.setText(identity[1] if identity else "nerozpoznána (bez připojení, nebo brána neodpovídá)")
        self.btn_bind.setEnabled(identity is not None and selected(self.table) is not None)
        online = sync is not None and sync.logged_in
        self.btn_sync.setEnabled(online)
        self.btn_conflicts.setVisible(online and bool(sync.pending_conflicts))
        detached = False
        pid = selected(self.table)
        if online and store is not None and pid:
            try:
                rec = store.record(pid)
                foreign = bool(rec.get("remote_id")) and (rec.get("remote_account"), rec.get("remote_server")) \
                    != (sync.account, sync.client.base_url)
                detached = bool(rec.get("local_only")) or foreign
            except (OSError, ValueError, KeyError):
                pass
        self.btn_reupload.setVisible(detached)

    def _warn(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Profily", str(exc))

    def _activate(self) -> None:
        pid = selected(self.table)
        if not pid:
            return
        try:
            self.ctrl.switch_profile(pid)
        except (OSError, ValueError, KeyError) as exc:
            self._warn(exc)
            return
        self.refresh()
        self.switched.emit()

    def _create(self) -> None:
        store = self.ctrl.profile_store
        name, ok = QInputDialog.getText(self, "Nový profil", "Název profilu (např. Firemní):")
        if not ok:
            return
        try:
            self.ctrl._save_timer.stop()
            self.ctrl._save_now()
            pid = store.create(name, self.ctrl.cfg.copy())
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.refresh(pid)
        self._activate()
        self.ctrl.profiles_changed.emit()

    def _rename(self) -> None:
        store, pid = self.ctrl.profile_store, selected(self.table)
        if not pid:
            return
        try:
            current = store.record(pid)["name"]
            name, ok = QInputDialog.getText(self, "Přejmenovat profil", "Název:", text=current)
            if not ok:
                return
            store.rename(pid, name)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.ctrl.profiles_changed.emit()

    def _delete(self) -> None:
        store, pid = self.ctrl.profile_store, selected(self.table)
        if not pid:
            return
        try:
            if pid == store.active_id():
                QMessageBox.information(self, "Profily", "Aktivní profil smazat nelze. Nejprve přepněte na jiný.")
                return
            rec = store.record(pid)
            extra = "\n\nProfil se smaže i ze serveru a tím ze všech vašich počítačů." if rec.get("remote_id") else ""
            if QMessageBox.question(self, "Smazat profil", f"Opravdu smazat profil „{rec['name']}“?{extra}") \
                    != QMessageBox.StandardButton.Yes:
                return
            store.delete(pid)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.ctrl.profiles_changed.emit()

    def _sync(self) -> None:
        if self.ctrl.sync is not None:
            self.ctrl.sync.sync_now()

    def _conflicts(self) -> None:
        sync = self.ctrl.sync
        if sync is not None and sync.pending_conflicts:
            ask_conflicts(sync, sync.pending_conflicts, self)

    def _reupload(self) -> None:
        store, pid = self.ctrl.profile_store, selected(self.table)
        if not pid:
            return
        try:
            store.unbind(pid)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return
        self.ctrl.profiles_changed.emit()


# ====================================================================== Účet
class AccountTab(QWidget):
    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        sync = ctrl.sync
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "Po přihlášení se profily automaticky ukládají na server a stahují na další vaše počítače. "
            "Bez přihlášení funguje NetMonitor dál jen lokálně. Nový účet lze založit pouze s licenčním klíčem "
            "od správce serveru. Heslo se nikam neukládá; na tomto počítači zůstává jen přihlašovací token."))
        form = QFormLayout()
        self.server = QLineEdit(sync.client.base_url if sync else "")
        self.email = QLineEdit(sync.settings["email"] if sync else "")
        self.email.setPlaceholderText("uzivatel@domena.cz")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.code = QLineEdit()
        self.code.setPlaceholderText("6 číslic z autentikátoru – jen pokud máte zapnuté dvoufázové ověření")
        self.code.setMaxLength(8)
        self.license = QLineEdit()
        self.license.setPlaceholderText("NM-XXXXX-XXXXX-XXXXX-XXXXX – jen pro registraci nového účtu")
        self.remember = QCheckBox("Zůstat přihlášen na tomto počítači")
        self.remember.setChecked(True)
        form.addRow("Server", self.server)
        form.addRow("E-mail", self.email)
        form.addRow("Heslo", self.password)
        form.addRow("Kód 2FA", self.code)
        form.addRow("Licenční klíč", self.license)
        form.addRow("", self.remember)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.btn_login = QPushButton("Přihlásit")
        self.btn_register = QPushButton("Registrovat nový účet")
        self.btn_logout = QPushButton("Odhlásit")
        self.btn_password = QPushButton("Změnit heslo…")
        self.btn_login.clicked.connect(self._login)
        self.btn_register.clicked.connect(self._register)
        self.btn_logout.clicked.connect(self._logout)
        self.btn_password.clicked.connect(self._change_password)
        self.password.returnPressed.connect(self._login)
        self.code.returnPressed.connect(self._login)
        for button in (self.btn_login, self.btn_register, self.btn_logout, self.btn_password):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = hint("")
        layout.addWidget(self.status)
        layout.addStretch(1)
        if sync is not None:
            sync.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        sync = self.ctrl.sync
        if sync is None:
            self.setEnabled(False)
            self.status.setText("Synchronizace není v tomto režimu dostupná.")
            return
        logged = sync.logged_in
        for widget in (self.server, self.email, self.password, self.code, self.license, self.remember,
                       self.btn_login, self.btn_register):
            widget.setEnabled(not logged and not sync.busy)
        self.btn_logout.setEnabled(logged)
        self.btn_password.setEnabled(logged)
        if logged:
            self.email.setText(sync.client.email)
        self.status.setText(sync.status)

    def _inputs(self) -> tuple[str, str] | None:
        sync = self.ctrl.sync
        email, password = self.email.text().strip(), self.password.text()
        if not email or not password:
            self.status.setText("Vyplňte e-mail a heslo.")
            return None
        try:
            sync.set_server(self.server.text())
        except ValueError as exc:
            self.status.setText(str(exc))
            return None
        return email, password

    def _login(self) -> None:
        if (data := self._inputs()) is None:
            return
        self.ctrl.sync.login(data[0], data[1], self.code.text().strip() or None, self.remember.isChecked(),
                             self._auth_done)

    def _register(self) -> None:
        if (data := self._inputs()) is None:
            return
        key = self.license.text().strip()
        if not key:
            self.status.setText("Pro registraci zadejte licenční klíč od správce.")
            self.license.setFocus()
            return
        if len(data[1]) < 10:
            self.status.setText("Heslo musí mít alespoň 10 znaků.")
            return
        again, ok = QInputDialog.getText(self, "Registrace", "Zadejte heslo znovu pro kontrolu:",
                                         QLineEdit.EchoMode.Password)
        if not ok:
            return
        if again != data[1]:
            self.status.setText("Hesla se neshodují.")
            return
        self.ctrl.sync.register(data[0], data[1], key, self.remember.isChecked(), self._auth_done)

    def _auth_done(self, error: SyncError | None) -> None:
        if error is None:
            self.password.clear()
            self.code.clear()
            self.license.clear()
        elif error.code in ("totp_required", "invalid_totp"):
            self.code.setFocus()
            self.code.selectAll()
        self.refresh()

    def _logout(self) -> None:
        self.ctrl.sync.logout()
        self.password.clear()
        self.refresh()

    def _change_password(self) -> None:
        sync = self.ctrl.sync
        current, ok = QInputDialog.getText(self, "Změna hesla", "Současné heslo:", QLineEdit.EchoMode.Password)
        if not ok or not current:
            return
        new, ok = QInputDialog.getText(self, "Změna hesla", "Nové heslo (alespoň 10 znaků):",
                                       QLineEdit.EchoMode.Password)
        if not ok:
            return
        again, ok = QInputDialog.getText(self, "Změna hesla", "Nové heslo znovu:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if new != again or len(new) < 10:
            self.status.setText("Nové heslo musí mít alespoň 10 znaků a obě zadání se musí shodovat.")
            return

        def done(result):
            self.status.setText(f"Heslo se nepodařilo změnit: {result}" if isinstance(result, Exception)
                                else "Heslo změněno. Ostatní zařízení byla odhlášena.")
        sync.run("password", lambda: sync.client.change_password(current, new), done)


# ====================================================================== Správa
class AdminTab(QWidget):
    """Licenční klíče a účty. Server tyto operace povolí jen správci přihlášenému s 2FA."""

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        layout = QVBoxLayout(self)

        self.totp_box = QGroupBox("Dvoufázové ověření správce (povinné)")
        tl = QVBoxLayout(self.totp_box)
        tl.addWidget(hint("Účet správce vydává licenční klíče, proto vyžaduje druhý faktor. V telefonu otevřete "
                          "autentikátor (Aegis, Google Authenticator, Microsoft Authenticator, Bitwarden…), "
                          "naskenujte QR kód (nebo zadejte klíč ručně) a potvrďte šestimístným kódem."))
        self.btn_totp = QPushButton("Zobrazit QR kód pro autentikátor")
        self.btn_totp.clicked.connect(self._totp_setup)
        tl.addWidget(self.btn_totp)
        self.totp_qr = QLabel()
        self.totp_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.totp_qr.hide()
        tl.addWidget(self.totp_qr)
        self.totp_secret = QLineEdit()
        self.totp_secret.setReadOnly(True)
        self.totp_secret.setPlaceholderText("klíč se zobrazí po vygenerování")
        copy_uri = QPushButton("Kopírovat odkaz otpauth://")
        copy_uri.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._totp_uri))
        srow = QHBoxLayout()
        srow.addWidget(self.totp_secret, 1)
        srow.addWidget(copy_uri)
        tl.addLayout(srow)
        self.totp_code = QLineEdit()
        self.totp_code.setPlaceholderText("kód z autentikátoru")
        self.totp_code.setMaxLength(8)
        confirm = QPushButton("Zapnout dvoufázové ověření")
        confirm.clicked.connect(self._totp_enable)
        self.totp_code.returnPressed.connect(self._totp_enable)
        crow = QHBoxLayout()
        crow.addWidget(self.totp_code, 1)
        crow.addWidget(confirm)
        tl.addLayout(crow)
        self._totp_uri = ""
        layout.addWidget(self.totp_box)

        self.keys_box = QGroupBox("Licenční klíče")
        kl = QVBoxLayout(self.keys_box)
        new = QHBoxLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText("Poznámka – pro koho klíč je (např. Pavel Novák, kancelář)")
        self.note.setMaxLength(200)
        self.uses = QSpinBox(minimum=1, maximum=1000)
        self.uses.setPrefix("účtů: ")
        self.uses.setToolTip("Kolik účtů lze tímto klíčem zaregistrovat")
        self.days = QSpinBox(minimum=0, maximum=3650)
        self.days.setSpecialValueText("bez omezení")
        self.days.setSuffix(" dní")
        self.days.setToolTip("Do kdy musí být klíč použit (0 = bez časového omezení)")
        create = QPushButton("Vytvořit klíč")
        create.clicked.connect(self._create_key)
        self.note.returnPressed.connect(self._create_key)
        new.addWidget(self.note, 1)
        new.addWidget(self.uses)
        new.addWidget(self.days)
        new.addWidget(create)
        kl.addLayout(new)
        shown = QHBoxLayout()
        self.new_key = QLineEdit()
        self.new_key.setReadOnly(True)
        self.new_key.setPlaceholderText("Nový klíč se zobrazí zde – jen jednou, server ukládá pouze jeho otisk.")
        copy = QPushButton("Kopírovat")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.new_key.text()))
        self.btn_invite = QPushButton("Kopírovat pozvánku")
        self.btn_invite.setToolTip("Zkopíruje hotovou zprávu pro kamaráda: odkaz ke stažení, postup registrace a klíč.")
        self.btn_invite.setEnabled(False)
        self.btn_invite.clicked.connect(self._copy_invitation)
        self._invitation = ""
        shown.addWidget(self.new_key, 1)
        shown.addWidget(copy)
        shown.addWidget(self.btn_invite)
        kl.addLayout(shown)
        self.keys = table(["Poznámka", "Klíč", "Stav", "Použito", "Vytvořen", "Platí do", "Účty"])
        kl.addWidget(self.keys, 1)
        revoke = QPushButton("Zneplatnit vybraný klíč")
        revoke.clicked.connect(self._revoke_key)
        kl.addWidget(revoke, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.keys_box, 3)

        self.users_box = QGroupBox("Účty")
        ul = QVBoxLayout(self.users_box)
        self.users = table(["E-mail", "Klíč (poznámka)", "Profilů", "Poslední přihlášení", "Stav"])
        ul.addWidget(self.users, 1)
        urow = QHBoxLayout()
        self.btn_block = QPushButton("Zablokovat / odblokovat vybraný účet")
        self.btn_block.clicked.connect(self._toggle_user)
        reload_ = QPushButton("Obnovit")
        reload_.clicked.connect(self.refresh)
        urow.addWidget(self.btn_block)
        urow.addWidget(reload_)
        urow.addStretch(1)
        ul.addLayout(urow)
        layout.addWidget(self.users_box, 2)
        self.status = hint("")
        layout.addWidget(self.status)

    @property
    def sync(self):
        return self.ctrl.sync

    def _fail(self, result) -> bool:
        if isinstance(result, Exception):
            self.status.setText(f"Chyba: {result}")
            return True
        return False

    def refresh(self) -> None:
        sync = self.sync
        if sync is None or not sync.logged_in or not sync.client.is_admin:
            return
        ready = bool(sync.client.user.get("totp_enabled")) and sync.client.mfa
        self.totp_box.setVisible(not ready)
        self.keys_box.setEnabled(ready)
        self.users_box.setEnabled(ready)
        if not ready:
            self.status.setText("Nejprve zapněte dvoufázové ověření. Do té doby server správu klíčů odmítá.")
            return
        client = sync.client
        sync.run("admin-load", lambda: (client.admin_keys(), client.admin_users()), self._loaded)

    def _loaded(self, result) -> None:
        if self._fail(result):
            return
        keys, users = result
        fill(self.keys, [(k, [k.get("note", ""), f"{k.get('prefix', '')}…", KEY_STATES.get(k.get("state"), "?"),
                              f"{k.get('uses', 0)} / {k.get('max_uses', 1)}", when(k.get("created_at")),
                              when(k.get("expires_at")) if k.get("expires_at") else "bez omezení",
                              ", ".join(k.get("users", [])) or "—"]) for k in keys])
        fill(self.users, [(u, [u.get("email", "") + ("  (správce)" if u.get("is_admin") else ""),
                               u.get("key_note", "") or "—", str(u.get("profiles", 0)),
                               when(u.get("last_login_at")),
                               "aktivní" if u.get("is_active") else "ZABLOKOVÁN"]) for u in users])
        active = sum(1 for k in keys if k.get("state") == "active")
        self.status.setText(f"Klíčů: {len(keys)} (platných {active}), účtů: {len(users)}.")

    # ------------------------------------------------------------------ 2FA
    def _totp_setup(self) -> None:
        client = self.sync.client

        def done(result):
            if self._fail(result):
                return
            secret = str(result.get("secret", ""))
            self._totp_uri = str(result.get("uri", ""))
            self.totp_secret.setText(" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
            try:
                self.totp_qr.setPixmap(qr_pixmap(self._totp_uri))
                self.totp_qr.show()
            except Exception as exc:  # noqa: BLE001 – bez QR kódu zůstává ruční zadání klíče
                self.totp_qr.hide()
                self.status.setText(f"QR kód nelze vykreslit ({exc}); zadejte klíč ručně.")
                return
            self.status.setText("Naskenujte QR kód autentikátorem a potvrďte šestimístným kódem.")
            self.totp_code.setFocus()
        self.sync.run("totp-setup", client.totp_setup, done)

    def _totp_enable(self) -> None:
        client, code = self.sync.client, self.totp_code.text().strip()
        if not code:
            return

        def done(result):
            if self._fail(result):
                return
            self.totp_code.clear()
            self.totp_secret.clear()
            self.totp_qr.clear()
            self.totp_qr.hide()
            self._totp_uri = ""
            self.sync.changed.emit()
            self.refresh()
        self.sync.run("totp-enable", lambda: client.totp_enable(code), done)

    # ------------------------------------------------------------------ klíče a účty
    def _create_key(self) -> None:
        note = self.note.text().strip()
        if not note:
            self.status.setText("Vyplňte poznámku, pro koho klíč je.")
            self.note.setFocus()
            return
        client, uses, days = self.sync.client, self.uses.value(), self.days.value() or None

        def done(result):
            if self._fail(result):
                return
            self.new_key.setText(str(result.get("key", "")))
            self.new_key.selectAll()
            self._invitation = invitation(result, client.base_url)
            self.btn_invite.setEnabled(True)
            self.note.clear()
            self.refresh()
        self.sync.run("key-create", lambda: client.admin_create_key(note, uses, days), done)

    def _copy_invitation(self) -> None:
        QGuiApplication.clipboard().setText(self._invitation)
        self.status.setText("Pozvánka je ve schránce – vložte ji do zprávy nebo e-mailu.")

    def _revoke_key(self) -> None:
        key = selected(self.keys)
        if not isinstance(key, dict):
            return
        if QMessageBox.question(self, "Zneplatnit klíč",
                                f"Zneplatnit klíč „{key.get('note', '')}“? Už s ním nepůjde registrovat. "
                                "Existující účty zůstanou – ty lze zablokovat zvlášť.") \
                != QMessageBox.StandardButton.Yes:
            return
        client = self.sync.client
        self.sync.run("key-revoke", lambda: client.admin_revoke_key(key["id"]),
                      lambda r: None if self._fail(r) else self.refresh())

    def _toggle_user(self) -> None:
        user = selected(self.users)
        if not isinstance(user, dict):
            return
        activate = not user.get("is_active")
        verb = "Odblokovat" if activate else "Zablokovat"
        if QMessageBox.question(self, f"{verb} účet", f"{verb} účet {user.get('email', '')}?"
                                + ("" if activate else " Všechna jeho zařízení budou odhlášena.")) \
                != QMessageBox.StandardButton.Yes:
            return
        client = self.sync.client
        self.sync.run("user-toggle", lambda: client.admin_set_user_active(user["id"], activate),
                      lambda r: None if self._fail(r) else self.refresh())
