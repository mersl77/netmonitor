import json

import pytest

from netmonitor.core import config as c


def test_defaults_have_ten_targets_from_spec():
    cfg = c.AppConfig.from_dict({})
    names = [t.name for t in cfg.ping.targets]
    assert len(names) == 10
    assert names[0] == "Zelina" and cfg.ping.targets[0].host == "zelina.eu"
    sb3 = next(t for t in cfg.ping.targets if t.name == "SB3 router")
    gp = next(t for t in cfg.ping.targets if t.name == "Greenpacket H5")
    assert sb3.host == gp.host == "192.168.0.1"          # stejná IP je povolená
    assert sb3.id != gp.id                                 # ... a záznamy se neslučují
    assert all(t.interval_s == 2.0 for t in cfg.ping.targets)


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    cfg = c.AppConfig.from_dict({})
    cfg.appearance.mode = c.MODE_ONTOP
    cfg.appearance.x, cfg.appearance.y = 100, 200
    cfg.ping.targets[0].interval_s = 5
    cfg.ping.targets.append(c.PingTarget(name="Nový", host="1.1.1.1"))
    c.save_config(cfg, p)
    loaded = c.load_config(p)
    assert loaded.appearance.mode == c.MODE_ONTOP
    assert (loaded.appearance.x, loaded.appearance.y) == (100, 200)
    assert loaded.ping.targets[0].interval_s == 5
    assert len(loaded.ping.targets) == 11
    assert [t.id for t in loaded.ping.targets] == [t.id for t in cfg.ping.targets]


def test_missing_file_gives_defaults(tmp_path):
    cfg = c.load_config(tmp_path / "nope.json")
    assert len(cfg.ping.targets) == 10


def test_corrupted_file_is_backed_up(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ broken", encoding="utf-8")
    cfg = c.load_config(p)
    assert len(cfg.ping.targets) == 10
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
    b.ping.targets[0].name = "Změna"
    b.appearance.colors.ok = "#000000"
    assert a.ping.targets[0].name == "Zelina"
    assert a.appearance.colors.ok != "#000000"


def test_export_import_full_and_targets_only(tmp_path):
    cfg = c.AppConfig.from_dict({})
    cfg.appearance.opacity = 0.5
    p = tmp_path / "exp.json"
    c.export_config(cfg, p)
    imp = c.import_config(p)
    assert imp.appearance.opacity == 0.5 and len(imp.ping.targets) == 10

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
    assert old.schema_version == 4
