"""Ochrana strojově specifických nastavení při synchronizaci."""
from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.core.sync_config import export_shared, merge_shared


def test_sync_transfers_ip_presets_but_excludes_device_and_window_details():
    cfg = AppConfig.from_dict({})
    cfg.network.adapter_name = "Ethernet 7"
    cfg.network.adapter_auto = False
    cfg.network.ip_profiles = [{"name": "Office", "interface": "eth0"}]
    cfg.appearance.x = 175
    cfg.appearance.y = 420
    cfg.ping.targets = [PingTarget(name="Server", host="10.0.1.20")]
    outgoing = export_shared(cfg)
    assert set(outgoing) == {"ping", "notifications", "network"}
    assert outgoing["ping"]["targets"][0]["host"] == "10.0.1.20"
    assert set(outgoing["network"]) == {"ip_profiles", "switch_ip_profile"}
    assert outgoing["network"]["ip_profiles"][0]["name"] == "Office"
    assert "appearance" not in outgoing


def test_merge_keeps_current_device():
    existing = AppConfig.from_dict({})
    existing.network.adapter_auto = False
    existing.network.adapter_name = "Wi-Fi"
    existing.appearance.x = 50
    existing.appearance.y = 90
    remote = {"ping": {"targets": [{"name": "Firemní", "host": "192.168.88.1"}]},
              "notifications": {"enabled": False},
              "network": {"adapter_name": "UNTRUSTED"},
              "appearance": {"x": 999}}
    merged = merge_shared(remote, existing)
    assert merged.ping.targets[0].name == "Firemní"
    assert merged.network.adapter_name == "Wi-Fi"
    assert merged.appearance.x == 50
    assert merged.appearance.y == 90
    assert merged.notifications.enabled is False


def test_roundtrip_ip_dns_and_legacy_cloud():
    cfg = AppConfig.from_dict({})
    cfg.network.ip_profiles = [{"name": "Pracovní", "dhcp": False,
        "ip": "192.168.2.10", "mask": "255.255.255.0", "gateway": "192.168.2.1",
        "dns_dhcp": False, "dns": ["1.1.1.1", "8.8.8.8"]}]
    remote = export_shared(cfg)
    restored = merge_shared(remote, AppConfig.from_dict({}))
    assert restored.network.ip_profiles == cfg.network.ip_profiles
    del remote["network"]
    assert merge_shared(remote, cfg).network.ip_profiles == cfg.network.ip_profiles
