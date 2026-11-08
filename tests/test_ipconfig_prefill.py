"""Regrese B11: bezpečné načtení konfigurace jiné než aktivní karty, výhradně s mocky."""
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from netmonitor.core.config import AppConfig
from netmonitor.core.ipconfig import IpSettings
from netmonitor.core.network_info import NetworkInfoProvider
from netmonitor.gui.controller import Controller
from netmonitor.gui.ipconfig_dialog import IpConfigDialog

from .fakes import FakePinger


DNS = ("192.0.2.53", "192.0.2.54", "192.0.2.55")


def wait_for(condition):
    end = time.monotonic() + 3
    while not condition() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert condition(), "Nedoběhla úloha GUI"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    import netmonitor.core.network_info as network
    import netmonitor.gui.controller as controller
    import netmonitor.platform as plat

    def addresses():
        return {name: {"ipv4": (ip,), "ipv4_masks": (mask,), "ipv6": (), "mac": "", "speed": None,
                       "is_up": True}
                for name, ip, mask in (("active-test", "192.0.2.20", "255.255.255.0"),
                                       ("other-test", "198.51.100.20", "255.255.0.0"))}

    def state(iface):
        return {"gateway": "198.51.100.1" if iface == "other-test" else "192.0.2.1",
                "dns": DNS, "dhcp": False, "dns_dhcp": False}

    fake = SimpleNamespace(
        adapter_details=lambda: {name: {"kind": "ethernet"} for name in addresses()},
        default_gateway=lambda iface=None: (state(iface or "active-test")["gateway"], iface or "active-test"),
        dns_servers=lambda iface: (DNS, "mock"), dhcp_enabled=lambda iface: False,
        ip_config_details=state,
    )
    monkeypatch.setattr(network, "_psutil_interfaces", addresses)
    monkeypatch.setattr(network, "route_source_ip", lambda: "192.0.2.20")
    monkeypatch.setattr(plat, "current", lambda: fake)
    monkeypatch.setattr(controller, "select_pinger", lambda: FakePinger())
    ctrl = Controller(AppConfig(), tmp_path / "config.json", tmp_path / "history.sqlite3")
    ctrl.net_provider = NetworkInfoProvider(fake)
    ctrl.network = ctrl.net_provider.snapshot()
    dialog = IpConfigDialog(ctrl)
    yield dialog, ctrl, fake
    dialog.close()
    ctrl.shutdown()


def test_other_adapter_prefills_real_prefix_gateway_and_all_dns(editor):
    """Regrese B11: neaktivní karta /16 nesmí dostat /24 ani prázdnou bránu/DNS."""
    dialog, ctrl, _ = editor
    assert ctrl.network.active.name == "active-test"
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert dialog.netmask.text() == "255.255.0.0"
    assert dialog.ip.text() == "198.51.100.20"
    assert dialog.gw.text() == "198.51.100.1"
    assert dialog.settings().dns_list == list(DNS)
    assert "/16" in dialog.current.text()
    assert dialog.btn_apply.isEnabled()


def test_snapshot_keeps_masks_for_nonactive_adapter(editor):
    """Regrese B11: i základní seznam rozhraní musí uchovat masky z psutil."""
    _, ctrl, _ = editor
    other = next(i for i in ctrl.network.interfaces if i.name == "other-test")
    assert other.ipv4_masks == ("255.255.0.0",)


@pytest.mark.parametrize("field", ["mask", "gateway", "dns", "dhcp", "dns_dhcp"])
def test_unknown_is_visible_and_blocks_apply_even_with_valid_profile(editor, monkeypatch, field):
    """Regrese B11: nezjištěno není prázdná hodnota a profil nesmí obejít ochranu."""
    dialog, ctrl, _ = editor
    known = ctrl.net_provider.ip_configuration("other-test")
    monkeypatch.setattr(ctrl.net_provider, "ip_configuration", lambda _: replace(known, **{field: None}))
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert "nezjištěno" in dialog.current.text()
    assert not dialog.btn_apply.isEnabled()
    ctrl.cfg.network.ip_profiles = [IpSettings(iface="other-test").to_profile("DHCP")]
    dialog._fill_profiles()
    dialog.profiles.setCurrentIndex(0)
    dialog._load_profile()
    assert not dialog.btn_apply.isEnabled()
    monkeypatch.setattr(ctrl, "apply_ip", lambda *a: pytest.fail("Nezjištěný stav nesmí měnit síť"))
    dialog._apply()


def test_known_empty_gateway_and_dns_are_distinct_from_unknown(editor, monkeypatch):
    """Regrese B11: ověřená absence brány/DNS neznamená selhání načítání."""
    dialog, ctrl, _ = editor
    known = ctrl.net_provider.ip_configuration("other-test")
    monkeypatch.setattr(ctrl.net_provider, "ip_configuration",
                        lambda _: replace(known, gateway="", dns=(), dhcp=True, dns_dhcp=True))
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert "nezjištěno" not in dialog.current.text()
    assert dialog.gw.text() == dialog.dns1.text() == ""
    assert dialog.btn_apply.isEnabled()


def test_loading_is_background_busy_and_late_result_cannot_overwrite_selection(editor, monkeypatch):
    """Regrese B11: načítání nesmí blokovat GUI ani přepsat nově vybranou kartu."""
    dialog, ctrl, _ = editor
    started, release = threading.Event(), threading.Event()
    original = ctrl.net_provider.ip_configuration
    main_thread = threading.get_ident()

    def delayed(iface):
        assert threading.get_ident() != main_thread
        if iface == "other-test":
            started.set()
            assert release.wait(3)
        return original(iface)

    monkeypatch.setattr(ctrl.net_provider, "ip_configuration", delayed)
    try:
        dialog.open_for("other-test")
        wait_for(started.is_set)
        assert dialog._busy and not dialog.btn_apply.isEnabled()
        assert not dialog.netmask.isEnabled()
        dialog.adapter.setCurrentIndex(dialog.adapter.findData("active-test"))
        wait_for(lambda: not dialog._busy)
        release.set()
        wait_for(lambda: not ctrl._busy)
        assert dialog.ip.text() == "192.0.2.20"
        assert dialog.netmask.text() == "255.255.255.0"
    finally:
        release.set()


def test_failed_details_load_unblocks_gui_but_keeps_apply_disabled(editor, monkeypatch):
    """Regrese B11: chyba čtení se musí zobrazit jako nezjištěný stav."""
    dialog, _, fake = editor

    def failure(_):
        raise OSError("mock failure")

    monkeypatch.setattr(fake, "ip_config_details", failure)
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert "nezjištěno" in dialog.current.text()
    assert not dialog.btn_apply.isEnabled()
    assert dialog.adapter.isEnabled()


def test_blank_mask_never_falls_back_to_24(editor):
    """Regrese B11: smazaná maska nesmí změnit /16 na implicitní /24."""
    dialog, _, _ = editor
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    dialog.netmask.clear()
    assert dialog.settings().mask == ""
    assert not dialog.btn_apply.isEnabled()


def test_confirmation_shows_prefix_gateway_dns_before_and_after_and_defaults_to_no(editor, monkeypatch):
    """Regrese B11: potvrzení ukáže původní i nové hodnoty před jakýmkoli zápisem."""
    dialog, ctrl, _ = editor
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    dialog.netmask.setText("24")
    dialog.gw.setText("198.51.100.2")
    dialog.dns1.setText("192.0.2.60")
    dialog.dns2.clear()
    shown = []

    def reject(box):
        shown.append(box.text())
        assert box.defaultButton() == box.button(QMessageBox.StandardButton.No)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", reject)
    monkeypatch.setattr(ctrl, "apply_ip", lambda *a: pytest.fail("Zamítnuté potvrzení nesmí měnit síť"))
    dialog._apply()
    assert len(shown) == 1
    text = shown[0]
    assert "Prefix: /16 → /24" in text
    assert "Brána: 198.51.100.1 → 198.51.100.2" in text
    assert "DNS: " + ", ".join(DNS) + " → 192.0.2.60" in text


@pytest.mark.parametrize("change", [dict(iface='bad"name'), dict(ip="invalid"), dict(mask=""),
                                   dict(mask="255.0.255.0"), dict(gateway="invalid"), dict(dns=["invalid"])])
def test_controller_rejects_invalid_settings_without_calling_platform(editor, monkeypatch, change):
    """Regrese B11: samotná hranice Controller.apply_ip odmítne neplatné vstupy."""
    _, ctrl, fake = editor
    monkeypatch.setattr(fake, "apply_ip", lambda *a: pytest.fail("Platforma nesmí dostat neplatný vstup"), raising=False)
    settings = IpSettings(iface="other-test", dhcp=False, ip="198.51.100.20", mask="16",
                          gateway="198.51.100.1", dns_dhcp=False, dns=list(DNS))
    for key, value in change.items():
        setattr(settings, key, value)
    results = []
    ctrl.apply_ip(settings, results.append)
    wait_for(lambda: bool(results))
    assert results[0][0] is False and results[0][1]


def test_guard_rejects_real_network_writes():
    """Pojistka AC-005 skutečně zachytí i přímé spuštění zakázaných příkazů."""
    import subprocess
    from netmonitor.platform import linux, macos, windows
    from .conftest import _install_network_change_guard
    for module in (linux, macos, windows):
        with pytest.raises(AssertionError, match="změnit nastavení sítě"):
            module.apply_ip(IpSettings(iface="other-test"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Zápis dosáhl mocku procesu"))
        _install_network_change_guard(patch)
        for args in (["nmcli", "connection", "modify", "test"], ["nmcli", "connection", "up", "test"],
                     ["netsh", "interface", "ipv4", "set", "address", "test"],
                     ["pkexec", "test"], ["networksetup", "-setdhcp", "test"]):
            with pytest.raises(AssertionError, match="změnit nastavení sítě"):
                subprocess.run(args, check=False)


def test_confirmed_settings_reach_only_mock_and_preserve_all_dns(editor, monkeypatch):
    """Regrese B11: potvrzený formulář zachová /16, bránu a všechny DNS, zápis je mock."""
    dialog, ctrl, fake = editor
    applied = []

    def apply(settings):
        applied.append(settings)
        return True, "mock only"

    monkeypatch.setattr(fake, "apply_ip", apply, raising=False)
    monkeypatch.setattr(QMessageBox, "exec", lambda _: QMessageBox.StandardButton.Yes)
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    dialog._apply()
    wait_for(lambda: bool(applied) and not dialog._busy)
    assert len(applied) == 1
    assert applied[0].prefix == 16 and applied[0].gateway == "198.51.100.1"
    assert applied[0].dns_list == list(DNS)
    assert not dialog.btn_apply.isEnabled()  # po zápisu se musí načíst nový aktuální stav


def test_profile_roundtrip_keeps_all_loaded_dns(editor):
    """Regrese B11: profil načtené karty nesmí tiše zkrátit DNS na dva servery."""
    dialog, ctrl, _ = editor
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    ctrl.cfg.network.ip_profiles = [dialog.settings().to_profile("Test DNS")]
    dialog._fill_profiles()
    dialog.profiles.setCurrentIndex(0)
    dialog.dns2.clear()
    dialog._load_profile()
    assert dialog.settings().dns_list == list(DNS)


def test_controller_validates_immutable_copy_before_background_write(editor, monkeypatch):
    """Regrese B11: volající nesmí změnit vstup mezi validací a během workeru."""
    _, ctrl, fake = editor
    queued, applied = [], []
    monkeypatch.setattr(ctrl, "_run_bg", lambda key, job, done: queued.append(job))
    monkeypatch.setattr(fake, "apply_ip", lambda settings: applied.append(settings), raising=False)
    settings = IpSettings(iface="other-test", dhcp=False, ip="198.51.100.20", mask="16",
                          gateway="198.51.100.1", dns_dhcp=False, dns=list(DNS))
    ctrl.apply_ip(settings, lambda _: None)
    settings.mask = "invalid"
    settings.dns[0] = "invalid"
    queued[0]()
    assert applied[0].prefix == 16 and applied[0].dns_list == list(DNS)


def test_reopen_during_apply_keeps_write_busy_and_defers_reload(editor, monkeypatch):
    """Regrese review 2: nové otevření nesmí uvolnit formulář během zápisu IP."""
    dialog, ctrl, _ = editor
    callbacks = []
    monkeypatch.setattr(QMessageBox, "exec", lambda _: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(ctrl, "apply_ip", lambda settings, done: callbacks.append(done))
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    dialog._apply()
    assert len(callbacks) == 1 and dialog._busy
    generation = dialog._load_generation
    dialog.close()
    dialog.open_for("active-test")
    assert dialog._load_generation == generation
    assert dialog.adapter.currentData() == "other-test"
    assert dialog._reload_pending and dialog._busy
    assert not dialog.btn_apply.isEnabled()
    dialog._set_busy(False)  # ani dokončení čtecí úlohy nesmí ukončit zápisový busy
    assert dialog._busy and not dialog.btn_apply.isEnabled()
    dialog._apply()
    assert len(callbacks) == 1
    callbacks[0]((True, "mock"))
    assert not dialog._busy and not dialog.btn_apply.isEnabled()
    ctrl.network_updated.emit(ctrl.network)
    wait_for(lambda: not dialog._busy)
    assert dialog.btn_apply.isEnabled() and not dialog._reload_pending


def test_controller_reports_concurrent_apply_instead_of_discarding_it(editor, monkeypatch):
    """Regrese review 2: druhý zápis musí dostat výsledek, ne se ztratit v _run_bg."""
    _, ctrl, fake = editor
    started, release = threading.Event(), threading.Event()
    calls, first, second = [], [], []

    def delayed(settings):
        calls.append(settings)
        started.set()
        assert release.wait(3)
        return False, "mock only"

    monkeypatch.setattr(fake, "apply_ip", delayed, raising=False)
    try:
        ctrl.apply_ip(IpSettings(iface="other-test"), first.append)
        wait_for(started.is_set)
        ctrl.apply_ip(IpSettings(iface="active-test"), second.append)
        assert second == [(False, "Probíhá jiná změna IP")]
        assert not first and len(calls) == 1
    finally:
        release.set()
        wait_for(lambda: bool(first))
    assert first == [(False, "mock only")] and len(calls) == 1


def test_multiple_ipv4_addresses_block_apply_even_after_profile_load(editor, monkeypatch):
    """Regrese review 2: editor nesmí tiše odstranit další IPv4 adresy karty."""
    import netmonitor.core.network_info as network
    dialog, ctrl, _ = editor
    original = network._psutil_interfaces

    def multiple():
        interfaces = original()
        interfaces["other-test"]["ipv4"] = ("198.51.100.20", "198.51.100.21")
        interfaces["other-test"]["ipv4_masks"] = ("255.255.0.0", "255.255.0.0")
        return interfaces

    monkeypatch.setattr(network, "_psutil_interfaces", multiple)
    monkeypatch.setattr(ctrl, "apply_ip", lambda *a: pytest.fail("Více IPv4 adres musí blokovat zápis"))
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert "karta má více IPv4 adres – změnu proveďte v nastavení systému" in dialog.msg.text()
    assert not dialog.btn_apply.isEnabled()
    ctrl.cfg.network.ip_profiles = [IpSettings(iface="other-test").to_profile("DHCP")]
    dialog._fill_profiles()
    dialog.profiles.setCurrentIndex(0)
    dialog._load_profile()
    assert not dialog.btn_apply.isEnabled()
    dialog._apply()


def test_ipv6_dns_blocks_apply_even_when_switching_to_automatic_dns(editor, monkeypatch):
    """Regrese review 2: nahrazení seznamu DNS nesmí skrytě odstranit IPv6 resolver."""
    dialog, ctrl, _ = editor
    known = ctrl.net_provider.ip_configuration("other-test")
    monkeypatch.setattr(ctrl.net_provider, "ip_configuration",
                        lambda _: replace(known, dns=DNS + ("2001:db8::53",)))
    monkeypatch.setattr(ctrl, "apply_ip", lambda *a: pytest.fail("IPv6 DNS musí blokovat zápis"))
    dialog.open_for("other-test")
    wait_for(lambda: not dialog._busy)
    assert "2001:db8::53" in dialog.current.text()
    assert not dialog.btn_apply.isEnabled()
    dialog.rb_dns_auto.setChecked(True)
    assert "IPv6 DNS" in dialog.msg.text()
    assert not dialog.btn_apply.isEnabled()
    dialog._apply()
