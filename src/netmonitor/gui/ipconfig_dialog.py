"""Dialog: nastavení IPv4 adresy a DNS vybrané síťové karty (DHCP / pevná IP) + uložené profily."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout)

from .. import platform as plat
from ..core.ipconfig import IpSettings, parse_mask
from ..core.network_info import InterfaceInfo
from ..core.ping_engine import Resolver, ping_host
from . import theme
from .controller import Controller
from .widgets import show_dialog

log = logging.getLogger(__name__)

DNS_PRESETS = [
    ("Vlastní…", None),
    ("Cloudflare – 1.1.1.1, 1.0.0.1", ["1.1.1.1", "1.0.0.1"]),
    ("Google – 8.8.8.8, 8.8.4.4", ["8.8.8.8", "8.8.4.4"]),
    ("Quad9 – 9.9.9.9, 149.112.112.112", ["9.9.9.9", "149.112.112.112"]),
]
KIND_LABEL = {"wifi": "Wi-Fi", "ethernet": "Ethernet", "vpn": "VPN", "other": "jiné"}


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("hint", True)
    return lab


class IpConfigDialog(QDialog):
    def __init__(self, ctrl: Controller, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.setWindowTitle("NetMonitor – Nastavení IP adresy síťové karty")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(560, 0)
        self._busy = False
        v = QVBoxLayout(self)

        top = QFormLayout()
        self.adapter = QComboBox()
        self.adapter.currentIndexChanged.connect(lambda _: self._load_adapter())
        top.addRow("Síťová karta", self.adapter)
        self.current = _hint("")
        top.addRow("Aktuálně", self.current)
        prof = QHBoxLayout()
        self.profiles = QComboBox()
        prof.addWidget(self.profiles, 1)
        for label, fn in (("Načíst", self._load_profile), ("Uložit jako…", self._save_profile),
                          ("Smazat", self._delete_profile)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            prof.addWidget(b)
        top.addRow("Profil", prof)
        v.addLayout(top)

        g_ip = QGroupBox("IP adresa (IPv4)")
        f_ip = QFormLayout(g_ip)
        self.rb_dhcp = QRadioButton("Automaticky (DHCP)")
        self.rb_static = QRadioButton("Ručně – pevná IP adresa")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_dhcp)
        grp.addButton(self.rb_static)
        r = QHBoxLayout()
        r.addWidget(self.rb_dhcp)
        r.addWidget(self.rb_static)
        r.addStretch(1)
        f_ip.addRow(r)
        self.ip = QLineEdit(placeholderText="např. 192.168.1.50")
        self.mask = QLineEdit(placeholderText="255.255.255.0 nebo 24")
        self.gw = QLineEdit(placeholderText="např. 192.168.1.1 (prázdné = bez brány)")
        f_ip.addRow("IP adresa", self.ip)
        f_ip.addRow("Maska / prefix", self.mask)
        f_ip.addRow("Výchozí brána", self.gw)
        v.addWidget(g_ip)

        g_dns = QGroupBox("DNS servery")
        f_dns = QFormLayout(g_dns)
        self.rb_dns_auto = QRadioButton("Automaticky")
        self.rb_dns_static = QRadioButton("Ručně")
        grp2 = QButtonGroup(self)
        grp2.addButton(self.rb_dns_auto)
        grp2.addButton(self.rb_dns_static)
        r2 = QHBoxLayout()
        r2.addWidget(self.rb_dns_auto)
        r2.addWidget(self.rb_dns_static)
        r2.addStretch(1)
        f_dns.addRow(r2)
        self.dns_preset = QComboBox()
        for label, _ in DNS_PRESETS:
            self.dns_preset.addItem(label)
        self.dns_preset.currentIndexChanged.connect(self._apply_dns_preset)
        f_dns.addRow("Předvolba", self.dns_preset)
        self.dns1 = QLineEdit(placeholderText="Primární DNS")
        self.dns2 = QLineEdit(placeholderText="Sekundární DNS (nepovinné)")
        f_dns.addRow("DNS 1", self.dns1)
        f_dns.addRow("DNS 2", self.dns2)
        v.addWidget(g_dns)

        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.msg)
        note = ("Změna vyžaduje práva správce – Windows zobrazí dotaz UAC." if sys.platform == "win32" else
                "Změna vyžaduje heslo správce." if sys.platform == "darwin" else
                "Změna proběhne přes NetworkManager (může se zeptat na heslo).")
        v.addWidget(_hint(note + " Spojení se při změně na několik sekund přeruší."))

        bot = QHBoxLayout()
        bot.addStretch(1)
        self.btn_apply = QPushButton("Použít")
        self.btn_apply.setProperty("accent", True)
        self.btn_apply.clicked.connect(self._apply)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        bot.addWidget(self.btn_apply)
        bot.addWidget(close)
        v.addLayout(bot)

        for rb in (self.rb_dhcp, self.rb_static, self.rb_dns_auto, self.rb_dns_static):
            rb.toggled.connect(self._update_enabled)
        for le in (self.ip, self.mask, self.gw, self.dns1, self.dns2):
            le.textChanged.connect(self._live_validate)
        ctrl.config_changed.connect(lambda _c: self._fill_profiles())
        self._reload_pending = False
        ctrl.network_updated.connect(self._on_network)

    def _on_network(self, _snap) -> None:
        if self._reload_pending and self.isVisible() and not self._busy:
            self._reload_pending = False
            name = self.adapter.currentData()
            self._fill_adapters(name)

    # ------------------------------------------------------------------ naplnění
    def open_for(self, iface: str | None = None) -> None:
        self._fill_adapters(iface)
        self._fill_profiles()
        self.msg.clear()
        show_dialog(self)

    def _interfaces(self) -> list[InterfaceInfo]:
        net = self.ctrl.network
        if not net:
            return []
        out = [i for i in net.interfaces if i.kind != "loopback" and not i.name.lower().startswith("lo")]
        if net.active and all(i.name != net.active.name for i in out):
            out.insert(0, net.active)
        return out

    def _fill_adapters(self, iface: str | None) -> None:
        net = self.ctrl.network
        want = iface or (net.active.name if net and net.active else None)
        self.adapter.blockSignals(True)
        self.adapter.clear()
        for i in self._interfaces():
            active = net and net.active and net.active.name == i.name
            kind = KIND_LABEL.get((net.active.kind if active else i.kind), i.kind)
            ip = i.ipv4[0] if i.ipv4 else "bez IPv4"
            self.adapter.addItem(f"{i.name}  ({kind}, {ip}){'  ● aktivní' if active else ''}", i.name)
        idx = max(0, self.adapter.findData(want)) if want else 0
        self.adapter.setCurrentIndex(idx)
        self.adapter.blockSignals(False)
        self._load_adapter()

    def _info(self, name: str) -> InterfaceInfo | None:
        net = self.ctrl.network
        if net and net.active and net.active.name == name:
            return net.active
        return next((i for i in self._interfaces() if i.name == name), None)

    def _load_adapter(self) -> None:
        name = self.adapter.currentData()
        info = self._info(name) if name else None
        try:
            dhcp = plat.current().dhcp_enabled(name) if name else None
        except Exception:  # noqa: BLE001
            log.debug("dhcp_enabled selhalo", exc_info=True)
            dhcp = None
        ip = info.ipv4[0] if info and info.ipv4 else ""
        mask = info.ipv4_masks[0] if info and info.ipv4_masks and info.ipv4_masks[0] else "255.255.255.0"
        gw = info.gateway if info and info.gateway else ""
        dns = [d for d in (info.dns if info else ()) if ":" not in d]
        try:
            prefix = f"/{parse_mask(mask)}" if ip else ""
        except ValueError:
            prefix = ""
        dhcp_txt = {True: "DHCP", False: "pevná IP", None: "režim nezjištěn"}[dhcp]
        self.current.setText(f"{ip or '–'}{prefix}, brána {gw or '–'}, DNS {', '.join(dns) or '–'}  ({dhcp_txt})")
        self.ip.setText(ip)
        self.mask.setText(mask)
        self.gw.setText(gw)
        self.dns1.setText(dns[0] if dns else "")
        self.dns2.setText(dns[1] if len(dns) > 1 else "")
        static = dhcp is False
        self.rb_static.setChecked(static)
        self.rb_dhcp.setChecked(not static)
        self.rb_dns_static.setChecked(static)
        self.rb_dns_auto.setChecked(not static)
        self.dns_preset.setCurrentIndex(0)
        self._update_enabled()

    def _fill_profiles(self) -> None:
        cur = self.profiles.currentText()
        self.profiles.clear()
        for p in self.ctrl.cfg.network.ip_profiles:
            self.profiles.addItem(p["name"])
        if cur:
            i = self.profiles.findText(cur)
            if i >= 0:
                self.profiles.setCurrentIndex(i)
        if not self.ctrl.cfg.network.ip_profiles:
            self.profiles.setPlaceholderText("žádné uložené profily")

    # ------------------------------------------------------------------ formulář
    def _update_enabled(self) -> None:
        static = self.rb_static.isChecked()
        for w in (self.ip, self.mask, self.gw):
            w.setEnabled(static)
        dns_static = self.rb_dns_static.isChecked()
        for w in (self.dns1, self.dns2, self.dns_preset):
            w.setEnabled(dns_static)
        self._live_validate()

    def _apply_dns_preset(self, i: int) -> None:
        vals = DNS_PRESETS[i][1]
        if vals:
            self.dns1.setText(vals[0])
            self.dns2.setText(vals[1])

    def settings(self) -> IpSettings:
        return IpSettings(iface=self.adapter.currentData() or "", dhcp=self.rb_dhcp.isChecked(),
                          ip=self.ip.text().strip(), mask=self.mask.text().strip() or "24",
                          gateway=self.gw.text().strip(), dns_dhcp=self.rb_dns_auto.isChecked(),
                          dns=[self.dns1.text().strip(), self.dns2.text().strip()])

    def _live_validate(self) -> tuple[list[str], list[str]]:
        errs, warns = self.settings().validate()
        html = "".join(f"<div style='color:#ff6b78'>✖ {e}</div>" for e in errs)
        html += "".join(f"<div style='color:#ffaa2b'>⚠ {w}</div>" for w in warns)
        self.msg.setText(html)
        self.btn_apply.setEnabled(not errs and not self._busy)
        return errs, warns

    # ------------------------------------------------------------------ profily
    def _load_profile(self) -> None:
        name = self.profiles.currentText()
        p = next((x for x in self.ctrl.cfg.network.ip_profiles if x["name"] == name), None)
        if not p:
            return
        s = IpSettings.from_profile(self.adapter.currentData() or "", p)
        self.rb_dhcp.setChecked(s.dhcp)
        self.rb_static.setChecked(not s.dhcp)
        self.ip.setText(s.ip)
        self.mask.setText(s.mask)
        self.gw.setText(s.gateway)
        self.rb_dns_auto.setChecked(s.dns_dhcp)
        self.rb_dns_static.setChecked(not s.dns_dhcp)
        self.dns1.setText(s.dns[0] if s.dns else "")
        self.dns2.setText(s.dns[1] if len(s.dns) > 1 else "")
        self._update_enabled()

    def _save_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Uložit profil", "Název profilu (např. Kancelář – pevná IP):",
                                        text=self.profiles.currentText())
        name = name.strip()
        if not ok or not name:
            return
        prof = self.settings().to_profile(name)
        cfg = self.ctrl.cfg.copy()
        cfg.network.ip_profiles = [p for p in cfg.network.ip_profiles if p["name"] != name] + [prof]
        self.ctrl.update_config(cfg)
        self._fill_profiles()
        self.profiles.setCurrentText(name)

    def _delete_profile(self) -> None:
        name = self.profiles.currentText()
        if not name:
            return
        if QMessageBox.question(self, "Smazat profil", f"Smazat profil '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        cfg = self.ctrl.cfg.copy()
        cfg.network.ip_profiles = [p for p in cfg.network.ip_profiles if p["name"] != name]
        self.ctrl.update_config(cfg)
        self._fill_profiles()

    # ------------------------------------------------------------------ použití
    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        self.btn_apply.setEnabled(not busy)
        self.btn_apply.setText("Pracuji…" if busy else "Použít")
        if text:
            self.msg.setText(f"<div style='color:#38d9ff'>{text}</div>")

    def _apply(self) -> None:
        s = self.settings()
        errs, warns = self._live_validate()
        if errs:
            return
        info = self._info(s.iface)
        own = info.ipv4 if info else ()
        if not s.dhcp and s.ip not in own:
            self._set_busy(True, f"Kontroluji, zda adresa {s.ip} není v síti obsazená…")
            pinger = self.ctrl.pinger
            self.ctrl._run_bg("ip-precheck", lambda: ping_host(s.ip, 0.8, pinger, Resolver()),
                              lambda r: self._confirm(s, warns, r.ok))
        else:
            self._confirm(s, warns, False)

    def _confirm(self, s: IpSettings, warns: list[str], occupied: bool) -> None:
        self._set_busy(False)
        text = s.summary()
        if occupied:
            text += f"\n\n⚠ Adresa {s.ip} už v síti odpovídá na ping – pravděpodobně je obsazená jiným zařízením!"
        if warns:
            text += "\n\n" + "\n".join(f"⚠ {w}" for w in warns)
        text += "\n\nSpojení se na několik sekund přeruší. Pokračovat?"
        box = QMessageBox(QMessageBox.Icon.Warning if occupied else QMessageBox.Icon.Question,
                          "Použít nastavení sítě", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                          self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self._live_validate()
            return
        self._set_busy(True, "Nastavuji síťovou kartu… (potvrďte dotaz správce)")
        self.ctrl.apply_ip(s, self._done)

    def _done(self, res) -> None:
        ok, out = res if isinstance(res, tuple) else (False, str(res))
        self._set_busy(False)
        if ok:
            self.msg.setText("<div style='color:#3dff6e'>✔ Nastavení bylo použito. Síťové údaje se za chvíli "
                             "obnoví ve widgetu.</div>")
            self.current.setText("aktualizuji…")
            self._reload_pending = True
        else:
            esc = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            self.msg.setText(f"<div style='color:#ff6b78'>✖ Nastavení se nepodařilo použít.</div>"
                             f"<div style='color:#8fb3d1;font-family:monospace;font-size:11px'>{esc[:2000]}</div>")
