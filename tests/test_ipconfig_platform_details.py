"""Čtení konfigurace pro editor: pouze syntetické výstupy a mockované platformní funkce."""
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from netmonitor.platform import linux, macos, windows


@pytest.mark.parametrize("method,custom,ignore,expected", [
    ("manual", "192.0.2.53", "no", False), ("auto", "", "no", True),
    ("auto", "192.0.2.53", "yes", False), ("auto", "192.0.2.53", "no", None),
])
def test_linux_per_adapter_modes_and_runtime_details(monkeypatch, method, custom, ignore, expected):
    """Regrese B11: režim DNS je nezávislý na DHCP a nesmí se použít globální DNS."""
    def read(args, **kwargs):
        if args[1:3] == ["-g", "GENERAL.CONNECTION"]:
            assert args[-1] == "other-test"
            return "test-profile"
        if args[-3:-1] == ["connection", "show"]:
            assert args[-1] == "test-profile"
            return f"ipv4.method:{method}\nipv4.dns:{custom}\nipv4.ignore-auto-dns:{ignore}\n"
        assert args[-3:] == ["device", "show", "other-test"]
        return "IP4.GATEWAY:198.51.100.1\nIP4.DNS[1]:192.0.2.53\nIP4.DNS[2]:192.0.2.54\n"

    monkeypatch.setattr(linux, "run_cmd", read)
    state = linux.ip_config_details("other-test")
    assert state["gateway"] == "198.51.100.1"
    assert state["dns"] == ("192.0.2.53", "192.0.2.54")
    assert state["dhcp"] is (method == "auto")
    assert state.get("dns_dhcp") is expected


@pytest.mark.parametrize("runtime,expected", [(None, {}), ("", {}),
                                            ("IP4.GATEWAY:--\n", {"gateway": "", "dns": ()})])
def test_linux_failure_is_distinct_from_known_empty(monkeypatch, runtime, expected):
    """Regrese B11: selhání NM nesmí znamenat známou absenci brány a DNS."""
    monkeypatch.setattr(linux, "_nm_connection", lambda _: "test-profile")
    monkeypatch.setattr(linux, "run_cmd", lambda args, **k: runtime if "device" in args else None)
    state = linux.ip_config_details("other-test")
    assert {key: value for key, value in state.items() if key in ("gateway", "dns")} == expected
    assert state["dhcp"] is None
    assert "dns_dhcp" not in state


def test_linux_unmanaged_interface_stays_unknown(monkeypatch):
    """Regrese B11: nespravovaná karta nesmí dostat odhadnutý stav jiné karty."""
    monkeypatch.setattr(linux, "_nm_connection", lambda _: None)
    monkeypatch.setattr(linux, "run_cmd", lambda *a, **k: pytest.fail("Nečekaný dotaz"))
    assert linux.ip_config_details("other-test") == {}


@pytest.mark.parametrize("dhcp,registry_dns,dns_auto", [(True, "192.0.2.53", False), (False, "", True)])
def test_windows_reads_selected_adapter_and_independent_dns_mode(monkeypatch, dhcp, registry_dns, dns_auto):
    """Regrese B11: Windows DNS režim nelze odvozovat z DHCP příznaku karty."""
    key_path = []

    def open_key(root, path):
        key_path.append(path)
        return nullcontext(object())

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        OpenKey=open_key, HKEY_LOCAL_MACHINE=object(), QueryValueEx=lambda *a: (registry_dns, 1)))
    monkeypatch.setattr(windows, "adapter_details", lambda: {
        "other-test": {"guid": "{TEST-GUID}", "gateways": ["198.51.100.1"],
                       "dns": ["192.0.2.53"], "dhcp": dhcp, "up": True, "ips": ["198.51.100.20"]},
        "active-test": {"gateways": ["192.0.2.1"], "dns": ["192.0.2.99"]}})
    state = windows.ip_config_details("other-test")
    assert state == {"gateway": "198.51.100.1", "dns": ("192.0.2.53",),
                     "dhcp": dhcp, "dns_dhcp": dns_auto}
    assert key_path[0].endswith("Interfaces\\{TEST-GUID}")


def test_windows_unavailable_data_are_unknown(monkeypatch):
    """Regrese B11: fallback bez režimu DNS musí editor zablokovat."""
    monkeypatch.setattr(windows, "adapter_details", lambda: {"other-test": {"dns": [], "gateways": []}})
    state = windows.ip_config_details("other-test")
    assert state == {"dhcp": None, "dns": None, "gateway": None}
    assert "dns_dhcp" not in state


@pytest.mark.parametrize("automatic", [False, True])
def test_macos_uses_only_selected_service_and_scoped_dns(monkeypatch, automatic):
    """Regrese B11: macOS nesmí jiné kartě přiřadit globální resolver aktivní karty."""
    monkeypatch.setattr(macos, "_service_for", lambda _: "Test Ethernet")

    def read(args, **kwargs):
        if args == ["scutil", "--dns"]:
            return ("resolver #1\n nameserver[0] : 192.0.2.99\n"
                    "DNS configuration (for scoped queries)\nresolver #1\n"
                    " nameserver[0] : 192.0.2.53\n if_index : 7 (other-test)\n")
        assert args[0] == "networksetup" and args[-1] == "Test Ethernet"
        if args[1] == "-getinfo":
            return "Manual Configuration\nIP address: 198.51.100.20\nRouter: 198.51.100.1\n"
        assert args[1] == "-getdnsservers"
        return "There aren't any DNS Servers set on Test Ethernet.\n" if automatic else "192.0.2.53\n"

    monkeypatch.setattr(macos, "run_cmd", read)
    state = macos.ip_config_details("other-test")
    assert state == {"dhcp": False, "gateway": "198.51.100.1", "dns_dhcp": automatic,
                     "dns": ("192.0.2.53",)}


def test_macos_global_dns_fallback_is_not_configuration_of_selected_adapter(monkeypatch):
    """Regrese B11: chybějící scoped resolver znamená nezjištěno, nikoli DNS jiné karty."""
    monkeypatch.setattr(macos, "_service_for", lambda _: "Test Ethernet")
    monkeypatch.setattr(macos, "run_cmd", lambda args, **k:
                        "There aren't any DNS Servers set on Test Ethernet." if "-getdnsservers" in args else
                        "resolver #1\n nameserver[0] : 192.0.2.99\n" if args[0] == "scutil" else
                        "DHCP Configuration\nRouter: none\n")
    state = macos.ip_config_details("other-test")
    assert state["gateway"] == "" and state["dns_dhcp"] is True
    assert "dns" not in state


@pytest.mark.parametrize("up,ips", [(False, ["198.51.100.20"]), (False, []),
                                   (True, []), (True, ["2001:db8::20"])])
def test_windows_disconnected_or_without_ipv4_does_not_claim_empty_gateway_dns(monkeypatch, up, ips):
    """Regrese review 2: prázdná runtime data odpojené Windows karty jsou nezjištěná."""
    monkeypatch.setattr(windows, "adapter_details", lambda: {
        "other-test": {"up": up, "ips": ips, "gateways": [], "dns": [], "dhcp": True}})
    state = windows.ip_config_details("other-test")
    assert state.get("gateway") is None and state.get("dns") is None


def test_macos_null_router_and_ipv6_dns_are_preserved(monkeypatch):
    """Regrese review 2: (null) není brána a IPv6 DNS nesmí zmizet ze stavu karty."""
    monkeypatch.setattr(macos, "_service_for", lambda _: "Test Ethernet")
    monkeypatch.setattr(macos, "run_cmd", lambda args, **k:
                        "Manual Configuration\nRouter: (null)\n" if "-getinfo" in args else
                        "192.0.2.53\n2001:db8::53\n")
    state = macos.ip_config_details("other-test")
    assert state["gateway"] == ""
    assert state["dns"] == ("192.0.2.53", "2001:db8::53")


def test_macos_scoped_ipv6_dns_are_preserved(monkeypatch):
    """Regrese review 2: i automatický scoped resolver musí zachovat IPv6 DNS."""
    monkeypatch.setattr(macos, "_service_for", lambda _: "Test Ethernet")
    monkeypatch.setattr(macos, "run_cmd", lambda args, **k:
                        "DHCP Configuration\nRouter: none\n" if "-getinfo" in args else
                        "There aren't any DNS Servers set on Test Ethernet." if "-getdnsservers" in args else
                        "DNS configuration (for scoped queries)\nresolver #1\n"
                        " nameserver[0] : 2001:db8::53\n if_index : 7 (other-test)\n")
    state = macos.ip_config_details("other-test")
    assert state["dns"] == ("2001:db8::53",)
