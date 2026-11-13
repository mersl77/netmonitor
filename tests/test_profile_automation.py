"""QR kód pro 2FA, pozvánka, IP předvolba při přepnutí profilu a nabídka profilu podle sítě."""
import pytest
from PySide6.QtWidgets import QMessageBox

from netmonitor.core import qr
from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.core.network_identity import identify
from netmonitor.core.network_info import InterfaceInfo, NetworkSnapshot
from netmonitor.core.profiles import ProfileStore
from netmonitor.gui import account_tabs, profile_prompts

from .sync_server_fake import PASSWORD, URL, server  # noqa: F401
from .test_gui_smoke import ctrl, pump, qapp  # noqa: F401
from .test_profile_workflow import dialog, env, login, select  # noqa: F401

PRESET = {"name": "Kancelář", "dhcp": False, "ip": "10.20.30.2", "mask": "255.255.255.0",
          "gateway": "10.20.30.1", "dns_dhcp": False, "dns": ["1.1.1.1"]}


def snap(kind="ethernet", ssid=None, gateway="192.168.1.1", ip="192.168.1.50"):
    iface = InterfaceInfo(name="eth0", display_name="Ethernet", kind=kind, is_up=True, ipv4=(ip,),
                          ssid=ssid, gateway=gateway)
    return NetworkSnapshot(active=iface, interfaces=(iface,), has_default_route=True, default_iface="eth0")


# ---------------------------------------------------------------- QR kód
def test_qr_matrix_is_a_valid_square_with_finder_patterns():
    m = qr.matrix("otpauth://totp/NetMonitor:a%40example.org?secret=JBSWY3DPEHPK3PXP&issuer=NetMonitor")
    assert len(m) >= 21 and all(len(row) == len(m) for row in m)
    finder = [[True] * 7, [True] + [False] * 5 + [True]]
    assert m[0][:7] == finder[0] and m[1][:7] == finder[1]            # levý horní roh
    assert m[0][-7:] == finder[0] and m[-1][:7] == finder[0]          # pravý horní a levý dolní


def test_qr_decodes_back_to_the_same_text(qapp, tmp_path):
    cv2 = pytest.importorskip("cv2")
    text = "otpauth://totp/NetMonitor:marcel%40zelina.eu?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=NetMonitor&digits=6&period=30"
    path = tmp_path / "qr.png"
    assert account_tabs.qr_pixmap(text).save(str(path))
    decoded, _points, _raw = cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(path)))
    assert decoded == text


def test_totp_setup_shows_qr(env, server):
    ctrl, _store = env
    server.add_user("admin@example.org", admin=True)
    login(ctrl, "admin@example.org")
    d = dialog(ctrl)
    admin = d.admin_tab
    assert admin.totp_qr.isHidden()
    admin._totp_setup()
    assert not admin.totp_qr.isHidden() and admin.totp_qr.pixmap().width() > 150
    admin.totp_code.setText(server.code(admin.totp_secret.text().replace(" ", "")))
    admin._totp_enable()
    assert admin.totp_qr.isHidden() and admin.keys_box.isEnabled()
    d.close()


# ---------------------------------------------------------------- pozvánka
def test_invitation_text_and_button(env, server, qapp):
    ctrl, _store = env
    secret = server.add_admin()
    login(ctrl, "admin@example.org", server.code(secret))
    d = dialog(ctrl)
    admin = d.admin_tab
    assert not admin.btn_invite.isEnabled()
    admin.note.setText("Pavel")
    admin.days.setValue(14)
    admin._create_key()
    key = admin.new_key.text()
    assert admin.btn_invite.isEnabled()
    admin._copy_invitation()
    text = qapp.clipboard().text()
    assert key in text and URL in text and "Registrovat nový účet" in text
    assert "releases/tag/v" in text and "pro jeden účet" in text and "použít do" in text
    assert "Pavel" not in text                     # poznámka je jen pro správce
    d.close()
    assert "pro 3 účtů" in account_tabs.invitation({"key": "NM-X", "max_uses": 3}, URL)


# ---------------------------------------------------------------- IP předvolba při přepnutí
def test_switch_offers_ip_preset_only_when_configured(env, monkeypatch):
    ctrl, store = env
    work = AppConfig.from_dict({})
    work.ping.targets = [PingTarget("Firma", "10.20.30.1")]
    work.network.ip_profiles = [dict(PRESET)]
    work.network.switch_ip_profile = "Kancelář"
    work_id, home_id = store.create("Firemní", work), store.active_id()
    offered = []
    ctrl.ip_preset_offered.connect(offered.append)
    ctrl.switch_profile(work_id)
    assert offered == ["Kancelář"]
    ctrl.switch_profile(home_id)
    assert offered == ["Kancelář"]                 # Soukromý žádnou předvolbu nemá

    opened = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    ctrl.switch_profile(work_id)
    profile_prompts.offer_ip_preset(ctrl, "Kancelář", lambda iface, preset: opened.append((iface, preset)))
    assert opened == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    profile_prompts.offer_ip_preset(ctrl, "Kancelář", lambda iface, preset: opened.append((iface, preset)))
    assert len(opened) == 1 and opened[0][1] == "Kancelář"
    profile_prompts.offer_ip_preset(ctrl, "Neexistuje", lambda *a: opened.append(a))
    assert len(opened) == 1


def test_preset_is_loaded_into_ip_dialog_without_applying(env, monkeypatch):
    ctrl, _store = env
    from netmonitor.gui.ipconfig_dialog import IpConfigDialog
    cfg = ctrl.cfg.copy()
    cfg.network.ip_profiles = [dict(PRESET)]
    ctrl.update_config(cfg)
    applied = []
    monkeypatch.setattr(ctrl, "apply_ip", lambda *a: applied.append(a))
    d = IpConfigDialog(ctrl)
    d.open_for(None, "Kancelář")
    assert d.ip.text() == "10.20.30.2" and d.gw.text() == "10.20.30.1" and d.dns1.text() == "1.1.1.1"
    assert d.rb_static.isChecked() and "Kancelář" in d.msg.text() and applied == []
    d.close()


def test_preset_choice_in_profiles_tab_and_cleanup_when_preset_removed(env):
    ctrl, _store = env
    cfg = ctrl.cfg.copy()
    cfg.network.ip_profiles = [dict(PRESET)]
    ctrl.update_config(cfg)
    d = dialog(ctrl)
    tab = d.profiles_tab
    assert [tab.ip_preset.itemText(i) for i in range(tab.ip_preset.count())][1:] == ["Kancelář"]
    tab.ip_preset.setCurrentIndex(1)
    tab._set_ip_preset(1)
    assert ctrl.cfg.network.switch_ip_profile == "Kancelář"
    cfg = ctrl.cfg.copy()
    cfg.network.ip_profiles = []
    ctrl.update_config(cfg)
    assert ctrl.cfg.network.switch_ip_profile == "" and tab.ip_preset.count() == 1
    d.close()


# ---------------------------------------------------------------- profil podle sítě
def test_network_identity():
    assert identify(None) is None
    assert identify(NetworkSnapshot(active=None)) is None
    assert identify(snap("wifi", "Firma-WiFi"), arp=lambda ip: None) == ("wifi:Firma-WiFi", "Wi-Fi „Firma-WiFi“")
    home = identify(snap(), arp=lambda ip: "AA:BB:CC:00:00:01")
    work = identify(snap(), arp=lambda ip: "aa:bb:cc:00:00:02")
    assert home[0] == "lan:192.168.1.1|aa:bb:cc:00:00:01" and home[0] != work[0]   # stejná IP brány, jiná síť
    assert identify(snap(), arp=lambda ip: None) is None
    assert identify(snap(gateway=None), arp=lambda ip: "aa:bb:cc:00:00:01") is None


def test_network_binding_is_exclusive_and_local(tmp_path):
    store = ProfileStore(tmp_path / "p")
    store.initialize(AppConfig.from_dict({}))
    home = store.active_id()
    work = store.create("Firemní", AppConfig.from_dict({}))
    store.bind_network(home, "wifi:Doma", "Wi-Fi „Doma“")
    store.bind_network(work, "wifi:Firma", "Wi-Fi „Firma“")
    store.bind_network(work, "wifi:Doma", "Wi-Fi „Doma“")          # přesun sítě k jinému profilu
    assert store.profile_for_network("wifi:Doma") == work and "networks" not in store.record(home)
    assert store.profile_for_network("wifi:Jinde") is None
    store.unbind_networks(work)
    assert store.profile_for_network("wifi:Firma") is None
    with pytest.raises(ValueError):
        store.bind_network(work, "", "x")
    # do synchronizace se spojení se sítí nedostane
    from netmonitor.core import sync_engine
    store.bind_network(work, "wifi:Firma", "Wi-Fi „Firma“")
    before = sync_engine.snapshot(store, "a@example.org", URL).profiles[1].hash
    store.unbind_networks(work)
    assert sync_engine.snapshot(store, "a@example.org", URL).profiles[1].hash == before


def test_profile_is_suggested_once_per_network_change(env, monkeypatch):
    ctrl, store = env
    import netmonitor.core.network_identity as ni
    macs = {"192.168.1.1": "aa:aa:aa:00:00:01", "10.20.30.1": "bb:bb:bb:00:00:02"}
    monkeypatch.setattr(ni, "arp_lookup", lambda ip: macs.get(ip))
    monkeypatch.setattr(ni.identify, "__defaults__", (lambda ip: macs.get(ip),))
    home_id = store.active_id()
    work_id = store.create("Firemní", AppConfig.from_dict({}))
    suggested = []
    ctrl.profile_suggested.connect(lambda pid, name, net: suggested.append((pid, name)))

    home_net, work_net = snap(), snap(gateway="10.20.30.1", ip="10.20.30.50")
    ctrl._on_network(home_net)
    assert ctrl.net_identity[0] == "lan:192.168.1.1|aa:aa:aa:00:00:01" and suggested == []
    d = dialog(ctrl)
    tab = d.profiles_tab
    assert "192.168.1.1" in tab.net_label.text()
    select(tab, home_id)
    tab._bind_network()
    ctrl._on_network(work_net)
    select(tab, work_id)
    tab._bind_network()
    assert tab.table.item(0, 1).text() == "síť s bránou 192.168.1.1"
    assert tab.table.item(1, 1).text() == "síť s bránou 10.20.30.1"
    assert suggested == []                         # spojení samo nic nenabízí

    ctrl._on_network(home_net)                     # návrat domů, aktivní je Soukromý → není co nabízet
    assert suggested == []
    ctrl.switch_profile(work_id)
    ctrl._on_network(work_net)                     # ve firmě s aktivním Firemním → nic
    assert suggested == []
    ctrl._on_network(home_net)                     # doma s aktivním Firemním → nabídka
    assert suggested == [(home_id, "Soukromý")]
    ctrl._on_network(home_net)
    ctrl._on_network(NetworkSnapshot(active=None)) # krátký výpadek
    ctrl._on_network(home_net)
    assert len(suggested) == 1                     # tatáž síť se neptá znovu
    ctrl._on_network(work_net)
    ctrl._on_network(home_net)
    assert len(suggested) == 2
    select(tab, home_id)
    tab._unbind_network()
    assert store.profile_for_network("lan:192.168.1.1|aa:aa:aa:00:00:01") is None
    d.close()


def test_suggestion_prompt_switches_only_on_confirmation(env, monkeypatch):
    ctrl, store = env
    work_id = store.create("Firemní", AppConfig.from_dict({}))
    home_id = store.active_id()
    monkeypatch.setattr(QMessageBox, "show", lambda self: None)
    profile_prompts.suggest_profile(ctrl, work_id, "Firemní", "Wi-Fi „Firma“")
    box = profile_prompts._open[-1]
    assert not box.isModal() and "Firemní" in box.text()
    box.done(0)                                    # zavřeno bez volby
    assert store.active_id() == home_id and box not in profile_prompts._open

    profile_prompts.suggest_profile(ctrl, work_id, "Firemní", "Wi-Fi „Firma“")
    box = profile_prompts._open[-1]
    next(b for b in box.buttons() if b.text() == "Přepnout").click()
    assert store.active_id() == work_id
