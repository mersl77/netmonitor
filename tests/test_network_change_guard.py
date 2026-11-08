"""Regrese review 2: pojistka propustí čtení, zápisy odmítne před mockovaným Popen."""
import subprocess
from unittest.mock import Mock

import pytest

from .conftest import _install_network_change_guard


@pytest.fixture
def guarded_spawn():
    # Znovu instalovat skutečnou pojistku, ale pod ni podložit mock procesu.
    # Ani při chybě pojistky se žádný příkaz nespustí.
    with pytest.MonkeyPatch.context() as patch:
        process = object()
        popen = Mock(return_value=process)
        patch.setattr(subprocess, "Popen", popen)
        _install_network_change_guard(patch)
        yield subprocess.Popen, popen, process


@pytest.mark.parametrize("args", [
    ["networksetup", "-listallhardwareports"], ["/usr/sbin/networksetup", "-getinfo", "Test Ethernet"],
    ["networksetup", "-getdnsservers", "Test Ethernet"],
    ["netsh", "wlan", "show", "interfaces"], ["netsh.exe", "interface", "ipv4", "show", "addresses"],
    ["nmcli", "con", "show", "up"], ["nmcli", "-t", "dev", "show", "other-test"],
    ["osascript", "-e", 'return "mock"'],
])
def test_guard_allows_read_only_commands(guarded_spawn, args):
    """Regrese review 2: čtecí networksetup/netsh nesmí rozbít integrační testy OS."""
    spawn, popen, process = guarded_spawn
    assert spawn(args) is process
    popen.assert_called_once_with(args)


@pytest.mark.parametrize("args", [
    ["networksetup", "-setmanual", "Test Ethernet", "198.51.100.20", "255.255.0.0", "198.51.100.1"],
    ["networksetup", "-createnetworkservice", "Test", "other-test"],
    ["networksetup", "-removenetworkservice", "Test"],
    ["networksetup", "-addadditionalroutes", "Test"],
    ["networksetup", "-deletepppoeservice", "Test"],
    ["netsh", "interface", "ipv4", "set", "address", "other-test"],
    ["netsh.exe", "interface", "ipv6", "add", "dnsservers", "other-test"],
    ["netsh", "interface", "ipv4", "delete", "address", "other-test"],
    ["netsh", "interface", "ip", "reset"],
    ["pkexec", "mock"],
    ["osascript", "-e", 'do shell script "mock" with administrator privileges'],
])
def test_guard_blocks_writes_before_process_creation(guarded_spawn, args):
    """Regrese review 2: zapisující nástroje nesmí dosáhnout ani mocku subprocessu."""
    spawn, popen, _ = guarded_spawn
    with pytest.raises(AssertionError, match="změnit nastavení sítě"):
        spawn(args)
    popen.assert_not_called()


@pytest.mark.parametrize("obj", ["connection", "con", "c", "device", "dev", "d"])
@pytest.mark.parametrize("action", ["modify", "mod", "up", "reapply", "connect"])
def test_guard_blocks_nmcli_abbreviations(guarded_spawn, obj, action):
    """Regrese review 2: zkrácené příkazy nmcli nesmí obejít pojistku zápisu."""
    spawn, popen, _ = guarded_spawn
    with pytest.raises(AssertionError, match="změnit nastavení sítě"):
        spawn(["nmcli", obj, action, "other-test"])
    popen.assert_not_called()
