import ipaddress
import json

import pytest

from netmonitor.core import config as c


def test_defaults_have_neutral_valid_targets():
    """Regrese L03: nová instalace nesmí pingovat privátní ani duplicitní cíle."""
    cfg = c.AppConfig.from_dict({})
    assert [t.name for t in cfg.ping.targets if t.is_section] == ["Internet", "Místní síť"]
    targets = cfg.real_targets()
    hosts = [t.host for t in targets]
    assert hosts == ["1.1.1.1", "8.8.8.8", "cloudflare.com"]
    assert len(hosts) == len(set(h.lower().rstrip(".") for h in hosts))
    for host in hosts:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        assert address.is_global
    assert all(t.validate() == [] for t in cfg.ping.targets)
    assert all(t.enabled and t.show_rtt and t.internet_probe == "auto" for t in targets)
    assert all(t.interval_s == 2.0 for t in cfg.ping.targets)
    assert cfg.network.gateway_probe is True


@pytest.mark.parametrize("schema_version", [1, 2, c.SCHEMA_VERSION])
def test_load_legacy_targets_does_not_replace_or_rewrite_them(tmp_path, schema_version):
    """Regrese L03: uložené původní cíle včetně duplicit zůstávají beze změny."""
    # Historická sada; veřejné O2 DNS zastupují dokumentační adresy.
    # Žádné údaje z aktuální sítě uživatele.
    legacy = [
        ("Zelina", "zelina.eu", "globe", True),
        ("Seznam", "seznam.cz", "globe", True),
        ("O2 DNS Primár", "192.0.2.53", "dns", True),
        ("O2 DNS Sekundár", "198.51.100.53", "dns", True),
        ("Google DNS", "8.8.8.8", "dns", True),
        ("Huawei anténa", "192.168.1.1", "wifi", False),
        ("Greenpacket H5", "192.168.0.1", "wifi", False),
        ("SB3 router", "192.168.0.1", "router", False),
        ("O2 modem", "10.0.0.138", "modem", False),
        ("Smart Box", "10.0.1.138", "box", False),
    ]
    cfg = c.AppConfig.from_dict({"ping": {"targets": [
        {"name": n, "host": h, "icon": i, "show_rtt": r, "id": f"legacy-{index}"}
        for index, (n, h, i, r) in enumerate(legacy)
    ]}})
    cfg.ping.targets[0].enabled = False
    cfg.ping.targets[1].internet_probe = "no"
    cfg.ping.targets[2].internet_probe = "yes"
    data = cfg.to_dict()
    data["schema_version"] = schema_version
    p = tmp_path / "config.json"
    original = json.dumps(data, ensure_ascii=False).encode("utf-8")
    p.write_bytes(original)

    loaded = c.load_config(p)

    assert loaded.to_dict()["ping"] == data["ping"]
    assert p.read_bytes() == original


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    cfg = c.AppConfig.from_dict({})
    original_count = len(cfg.ping.targets)
    cfg.appearance.mode = c.MODE_ONTOP
    cfg.appearance.x, cfg.appearance.y = 100, 200
    cfg.real_targets()[0].interval_s = 5
    cfg.ping.targets.append(c.PingTarget(name="Nový", host="192.0.2.1"))
    c.save_config(cfg, p)
    loaded = c.load_config(p)
    assert loaded.appearance.mode == c.MODE_ONTOP
    assert (loaded.appearance.x, loaded.appearance.y) == (100, 200)
    assert loaded.real_targets()[0].interval_s == 5
    assert len(loaded.ping.targets) == original_count + 1
    assert [t.id for t in loaded.ping.targets] == [t.id for t in cfg.ping.targets]


def test_missing_file_gives_defaults(tmp_path):
    cfg = c.load_config(tmp_path / "nope.json")
    assert [(t.name, t.host, t.kind) for t in cfg.ping.targets] == [
        (t.name, t.host, t.kind) for t in c.default_targets()
    ]


def test_corrupted_file_is_backed_up(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ broken", encoding="utf-8")
    cfg = c.load_config(p)
    assert [(t.name, t.host, t.kind) for t in cfg.ping.targets] == [
        (t.name, t.host, t.kind) for t in c.default_targets()
    ]
    assert (tmp_path / "config.broken.json").exists()


def test_values_are_clamped_and_unknown_keys_ignored():
    cfg = c.AppConfig.from_dict({
        "appearance": {"opacity": 7, "width": 10, "mode": "weird", "unknown": 1},
        "ping": {"targets": [{"name": "x", "host": "8.8.8.8", "interval_s": 999, "timeout_s": -3, "retries": 0,
                              "icon": "nonsense", "foo": "bar"}]},
        "network": {"wan_interval_s": 1},
    })
    t = cfg.ping.targets[0]
    assert (t.interval_s, t.timeout_s, t.retries, t.icon) == (60, 0.2, 1, "auto")
    assert cfg.appearance.opacity == 1.0 and cfg.appearance.width == 260
    assert cfg.appearance.mode == c.MODE_DESKTOP
    assert cfg.network.wan_interval_s == 30


def test_explicit_empty_target_list_is_respected():
    cfg = c.AppConfig.from_dict({"ping": {"targets": []}})
    assert cfg.ping.targets == []


def test_duplicate_ids_get_new_id():
    cfg = c.AppConfig.from_dict({"ping": {"targets": [{"id": "a", "name": "1", "host": "1.1.1.1"},
                                                      {"id": "a", "name": "2", "host": "1.1.1.1"}]}})
    assert len(cfg.ping.targets) == 2
    assert cfg.ping.targets[0].id != cfg.ping.targets[1].id


def test_target_validation():
    assert c.PingTarget(name="ok", host="seznam.cz").validate() == []
    errs = c.PingTarget(name="", host="bad host", interval_s=2, timeout_s=3).validate()
    assert len(errs) == 3


def test_config_change_and_copy_isolated():
    a = c.AppConfig.from_dict({})
    b = a.copy()
    original_name = a.ping.targets[0].name
    b.ping.targets[0].name = "Změna"
    b.appearance.colors.ok = "#000000"
    assert a.ping.targets[0].name == original_name
    assert a.appearance.colors.ok != "#000000"


def test_export_import_full_and_targets_only(tmp_path):
    cfg = c.AppConfig.from_dict({})
    cfg.appearance.opacity = 0.5
    p = tmp_path / "exp.json"
    c.export_config(cfg, p)
    imp = c.import_config(p)
    assert imp.appearance.opacity == 0.5 and imp.to_dict()["ping"] == cfg.to_dict()["ping"]

    current = c.AppConfig.from_dict({})
    current.appearance.opacity = 0.7
    tp = tmp_path / "targets.json"
    tp.write_text(json.dumps([{"name": "A", "host": "1.1.1.1"}, {"name": "B", "host": "example.com"}]))
    imp2 = c.import_config(tp, current)
    assert [t.name for t in imp2.ping.targets] == ["A", "B"]
    assert imp2.appearance.opacity == 0.7          # zbytek nastavení zachován


def test_import_rejects_garbage(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps([{"nothing": 1}]))
    with pytest.raises(ValueError):
        c.import_config(p)


def test_migration_sets_schema_version():
    cfg = c.AppConfig.from_dict({"schema_version": 0})
    assert cfg.schema_version == c.SCHEMA_VERSION


def test_migration_v1_to_v2_glass_readability():
    old = c.AppConfig.from_dict({"schema_version": 1, "appearance": {"glass_alpha": 0.55}})
    assert old.appearance.glass_alpha == 1.0           # staré výchozí -> přes v2 (0.75) až na v3 (neprůhledné)
    custom = c.AppConfig.from_dict({"schema_version": 1, "appearance": {"glass_alpha": 0.4}})
    assert custom.appearance.glass_alpha == 0.4        # vlastní volba uživatele zůstává
    assert old.schema_version == 3
