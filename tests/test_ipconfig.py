import ctypes

import pytest

from netmonitor.core.config import AppConfig
from netmonitor.core.ipconfig import (IpSettings, applescript_admin, networksetup_args, nmcli_modify_args, parse_mask,
                                      sanitize_profiles, windows_netsh_args, windows_script)


def static(**kw):
    base = dict(iface="Ethernet 10Gb", dhcp=False, ip="192.168.14.50", mask="255.255.255.0",
                gateway="192.168.14.1", dns_dhcp=False, dns=["192.168.14.2", "192.168.14.3"])
    base.update(kw)
    return IpSettings(**base)


@pytest.mark.parametrize("m,p", [("255.255.255.0", 24), ("24", 24), ("/16", 16), ("255.255.255.252", 30),
                                 ("255.255.0.0", 16)])
def test_parse_mask(m, p):
    assert parse_mask(m) == p


@pytest.mark.parametrize("m", ["255.0.255.0", "33", "0", "abc", ""])
def test_parse_mask_invalid(m):
    with pytest.raises(ValueError):
        parse_mask(m)


def test_valid_static_and_dhcp():
    assert static().validate() == ([], [])
    assert IpSettings(iface="Wi-Fi").validate() == ([], [])


def test_validation_errors():
    errs, _ = static(ip="192.168.14.300").validate()
    assert errs
    errs, _ = static(ip="192.168.14.0").validate()
    assert any("broadcast" in e or "sítě" in e for e in errs)
    errs, _ = static(ip="192.168.14.255").validate()
    assert errs
    errs, _ = static(gateway="192.168.14.50").validate()
    assert any("Brána" in e for e in errs)
    errs, _ = static(dns=["8.8.8.x", ""]).validate()
    assert errs
    errs, _ = static(dns=["", ""]).validate()
    assert errs
    errs, _ = static(iface='bad"name').validate()
    assert errs
    errs, _ = static(ip="127.0.0.5").validate()
    assert errs


def test_warnings():
    _, warns = static(gateway="10.0.0.1").validate()
    assert any("není v síti" in w for w in warns)
    _, warns = static(gateway="").validate()
    assert warns
    _, warns = static(dns_dhcp=True).validate()
    assert warns


def test_windows_netsh_commands():
    cmds = windows_netsh_args(static())
    assert cmds[0] == ["interface", "ipv4", "set", "address", "Ethernet 10Gb", "source=static",
                       "address=192.168.14.50", "mask=255.255.255.0", "gateway=192.168.14.1"]
    assert cmds[1] == ["interface", "ipv4", "set", "dnsservers", "Ethernet 10Gb", "source=static",
                       "address=192.168.14.2", "register=primary", "validate=no"]
    assert cmds[2] == ["interface", "ipv4", "add", "dnsservers", "Ethernet 10Gb", "address=192.168.14.3",
                       "index=2", "validate=no"]
    d = windows_netsh_args(IpSettings(iface="Wi-Fi"))
    assert d == [["interface", "ipv4", "set", "address", "Wi-Fi", "source=dhcp"],
                 ["interface", "ipv4", "set", "dnsservers", "Wi-Fi", "source=dhcp"]]
    assert "gateway=none" in windows_netsh_args(static(gateway=""))[0]
    assert "mask=255.255.0.0" in windows_netsh_args(static(mask="16", gateway="192.168.0.1"))[0]


def test_windows_script_quoting():
    s = windows_script(static(iface="Karta s 'apostrofem'"), r"C:\Users\Marcel\x.log")
    assert "'Karta s ''apostrofem'''" in s            # PowerShell single-quote escaping
    assert "$log = 'C:\\Users\\Marcel\\x.log'" in s
    assert s.count("Invoke-Netsh @(") == 3
    assert "exit $fail" in s


def test_nmcli_and_macos_commands():
    m = nmcli_modify_args(static(), "Drátové připojení 1")
    assert m[0][:4] == ["nmcli", "connection", "modify", "Drátové připojení 1"]
    assert "192.168.14.50/24" in m[0] and "192.168.14.2,192.168.14.3" in m[0]
    assert m[1] == ["nmcli", "connection", "up", "Drátové připojení 1"]
    d = nmcli_modify_args(IpSettings(iface="eth0"), "c")[0]
    assert d[d.index("ipv4.method") + 1] == "auto"
    ns = networksetup_args(static(), 'Wi-Fi "x"')
    assert ns[0] == ["/usr/sbin/networksetup", "-setmanual", 'Wi-Fi "x"', "192.168.14.50", "255.255.255.0",
                     "192.168.14.1"]
    script = applescript_admin(ns)
    assert script.startswith('do shell script "') and script.endswith('with administrator privileges')
    assert '\\"x\\"' in script                         # uvozovky escapované pro AppleScript


def test_profiles_roundtrip_and_sanitize():
    p = static().to_profile("Kancelář")
    assert "iface" not in p
    back = IpSettings.from_profile("Wi-Fi", p)
    assert back.iface == "Wi-Fi" and back.ip == "192.168.14.50" and back.dns == ["192.168.14.2", "192.168.14.3"]
    clean = sanitize_profiles([p, {"name": ""}, "junk", {"name": "X", "evil": 1, "dhcp": True}])
    assert [c["name"] for c in clean] == ["Kancelář", "X"]
    assert "evil" not in clean[1]
    cfg = AppConfig.from_dict({"network": {"ip_profiles": [p, 5]}})
    assert len(cfg.network.ip_profiles) == 1


def test_shellexecuteinfo_layout_x64():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("jen 64bit")
    from netmonitor.platform.windows import _shellexecute_struct
    assert ctypes.sizeof(_shellexecute_struct()) == 112        # sizeof(SHELLEXECUTEINFOW) na x64


@pytest.mark.parametrize("dns,expected", [
    ([f"192.0.2.{i}" for i in range(1, 12)], [f"192.0.2.{i}" for i in range(1, 9)]),
    ("192.0.2.53", ["192.0.2.53"]), (None, []), (42, []),
])
def test_profile_dns_is_bounded_and_string_is_one_server(dns, expected):
    """Regrese review 2: profil má nejvýše osm DNS, řetězec není seznam znaků."""
    settings = IpSettings.from_profile("other-test", {"name": "Test", "dns": dns})
    assert settings.dns == expected
