"""Regrese B02: poškozené lokální JSONy nesmějí zastavit start ani přijít o zálohu."""
import json
import logging
from pathlib import Path

import pytest

from netmonitor.core import config as c
from netmonitor.core.wan_info import WanResult, WanState


BAD_JSON = [b"", b"{ broken", b'"invalid"', b"[]", b"null", b"false", b"42",
            b'[["general", {}]]', b"\xff\xfe\x00\x80"]


def load_local(path):
    return c.load_config(path) if path.name == "config.json" else WanState(path)


def assert_defaults(state):
    if isinstance(state, c.AppConfig):
        assert state.appearance == c.AppearanceConfig()
        assert len(state.ping.targets) == 10
    else:
        assert state.current is None and state.previous is None
        assert state.changed_at is None and state.isp_cache == {}


def assert_backup(path, payload):
    backups = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload
    assert not path.exists()


@pytest.mark.parametrize("filename", ["config.json", "wan_state.json"])
@pytest.mark.parametrize("payload", BAD_JSON)
def test_invalid_local_json_uses_defaults_and_preserves_bytes(tmp_path, caplog, filename, payload):
    """Regrese B02: prázdný/rozbitý JSON, skalár, pole i binární data se odloží beze změny."""
    path = tmp_path / filename
    path.write_bytes(payload)
    with caplog.at_level(logging.WARNING):
        state = load_local(path)
    assert_defaults(state)
    assert_backup(path, payload)
    assert any(r.levelno == logging.WARNING and str(path) in r.message for r in caplog.records)


@pytest.mark.parametrize("bad_fields,field_name", [
    ({"appearance": {"x": "invalid", "y": 100}}, "appearance.x"),
    ({"appearance": {"x": [], "y": {}}}, "appearance.x"),
    ({"appearance": {"x": 2**40}}, "appearance.x"),
    ({"appearance": {"y": True}}, "appearance.y"),
    ({"appearance": {"y": 1.5}}, "appearance.y"),
    *[({"appearance": {"colors": {"ok": value}}}, "appearance.colors.ok")
      for value in (None, 7, True, [], {})],
    ({"appearance": {"colors": []}}, "appearance.colors"),
    ({"general": "invalid"}, "general"),
    ({"network": {"adapter_name": []}}, "network.adapter_name"),
    ({"network": {"external_api": "false"}}, "network.external_api"),
    ({"network": {"ip_profiles": [{"name": "Test", "dns": 42}]}}, "network.ip_profiles"),
    ({"ping": {"thresholds": []}}, "ping.thresholds"),
    ({"ping": {"targets": "invalid"}}, "ping.targets"),
    ({"notifications": {"high_latency_ms": []}}, "notifications.high_latency_ms"),
])
def test_config_invalid_field_types_keep_other_settings(tmp_path, caplog, bad_fields, field_name):
    """Regrese R1: vadné pole dostane výchozí hodnotu, ostatní nastavení i soubor zůstanou."""
    data: dict = {"history": {"retention_days": 17}, "ping": {"targets": [
        {"id": "a", "name": "Test A", "host": "192.0.2.1"},
        {"id": "b", "name": "Test B", "host": "192.0.2.2"},
    ]}}
    for group, value in bad_fields.items():
        if isinstance(value, dict) and group in data:
            data[group].update(value)
        else:
            data[group] = value
    path = tmp_path / "config.json"
    payload = json.dumps(data).encode()
    path.write_bytes(payload)
    cfg = c.load_config(path)
    assert cfg.history.retention_days == 17
    if field_name != "ping.targets":
        assert [t.id for t in cfg.ping.targets] == ["a", "b"]
    defaults = c.AppConfig.from_dict({})
    if field_name == "ping.targets":
        assert [(t.name, t.host) for t in cfg.ping.targets] == [(t.name, t.host) for t in defaults.ping.targets]
    else:
        actual, default = cfg, defaults
        for part in field_name.split("."):
            actual, default = getattr(actual, part), getattr(default, part)
        assert actual == default
    assert field_name in caplog.text
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("bad_target", [
    5, None, [], {},
    {"host": ["PRIVATE_VALUE_FOR_LOG"]},
    {"host": "192.0.2.3", "enabled": "PRIVATE_VALUE_FOR_LOG"},
])
def test_bad_field_and_target_preserve_valid_targets(tmp_path, caplog, bad_target):
    """Regrese R1: vadná opacity ani jednotlivý cíl nezahodí zbytek konfigurace."""
    path = tmp_path / "config.json"
    payload = json.dumps({
        "schema_version": 3,
        "general": {"language": "en"},
        "appearance": {"opacity": "PRIVATE_VALUE_FOR_LOG", "height": 900},
        "ping": {"max_concurrent": 7, "targets": [
            {"id": "a", "name": "Test A", "host": "192.0.2.1"}, bad_target,
            {"id": "b", "name": "Test B", "host": "192.0.2.2"},
        ]},
    }).encode()
    path.write_bytes(payload)
    cfg = c.load_config(path)
    assert cfg.general.language == "en"
    assert cfg.appearance.opacity == c.AppearanceConfig().opacity
    assert cfg.appearance.height == 900 and cfg.ping.max_concurrent == 7
    assert [(t.id, t.host) for t in cfg.ping.targets] == [("a", "192.0.2.1"), ("b", "192.0.2.2")]
    assert "appearance.opacity" in caplog.text and "ping.targets[1]" in caplog.text
    assert "PRIVATE_VALUE_FOR_LOG" not in caplog.text
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_save_config_does_not_validate_existing_fields(tmp_path, monkeypatch):
    """Regrese R2: save ověřuje jen JSON objekt, nikoli typy jeho polí."""
    cfg = c.AppConfig.from_dict({"ping": {"targets": []}})
    path = tmp_path / "config.json"
    path.write_text('{"appearance": {"opacity": "x"}, "ping": {"targets": [5]}}', encoding="utf-8")

    def no_validation(*args, **kwargs):
        pytest.fail("save_config nesmí validovat pole existující konfigurace")

    monkeypatch.setattr(c.AppConfig, "from_dict", no_validation)
    c.save_config(cfg, path)
    assert json.loads(path.read_text(encoding="utf-8")) == cfg.to_dict()
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("payload", BAD_JSON)
def test_save_config_preserves_invalid_json_object(tmp_path, payload):
    """Regrese R2: nečitelný JSON objekt se i při přímém save nejprve odloží."""
    cfg = c.AppConfig.from_dict({"ping": {"targets": []}})
    path = tmp_path / "config.json"
    path.write_bytes(payload)
    c.save_config(cfg, path)
    backups = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == payload
    assert json.loads(path.read_text(encoding="utf-8")) == cfg.to_dict()


@pytest.mark.parametrize("bad_fields", [
    {"current": []}, {"previous": 42}, {"changed_at": "yesterday"},
    {"changed_at": float("inf")}, {"isp_cache": []},
    {"isp_cache": {"192.0.2.1": []}},
    {"isp_cache": {"192.0.2.1": "ab"}},
    {"isp_cache": {"192.0.2.1": ["ISP", "yesterday"]}},
    {"isp_cache": {"192.0.2.1": [[], 1]}},
    {"isp_cache": {"192.0.2.1": ["ISP", True]}},
])
def test_wan_invalid_fields_discard_entire_cache(tmp_path, bad_fields):
    """Regrese B02: cache se ověří celá před změnou stavu, včetně záznamů ISP."""
    path = tmp_path / "wan_state.json"
    payload = json.dumps({"current": "192.0.2.1", **bad_fields}).encode()
    path.write_bytes(payload)
    assert_defaults(WanState(path))
    assert_backup(path, payload)


@pytest.mark.parametrize("filename", ["config.json", "wan_state.json"])
def test_unreadable_json_is_backed_up(tmp_path, monkeypatch, filename, caplog):
    """Regrese B02: chyba čtení souboru není důvod k pádu startu."""
    path = tmp_path / filename
    payload = b'{"unreadable": true}'
    path.write_bytes(payload)
    original_open = Path.open

    def deny_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("Test: nelze číst")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_read)
    assert_defaults(load_local(path))
    assert_backup(path, payload)
    assert str(path) in caplog.text


def test_config_failed_backup_prevents_later_overwrite(tmp_path, monkeypatch, caplog):
    """Regrese B02: selhání zálohy nesmí změnit konfiguraci ani při pozdějším save."""
    path = tmp_path / "config.json"
    payload = b"{ broken"
    path.write_bytes(payload)

    def deny_rename(*args, **kwargs):
        raise PermissionError("Test: nelze odložit")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", deny_rename)
        cfg = c.load_config(path)
        assert_defaults(cfg)
        with pytest.raises(OSError):
            c.save_config(cfg, path)
        assert path.read_bytes() == payload
        assert "Test: nelze odložit" in caplog.text
    c.save_config(cfg, path)
    assert list(tmp_path.glob("config.json.corrupt-*"))[0].read_bytes() == payload
    assert len(c.load_config(path).ping.targets) == 10


def test_repeated_corruption_does_not_replace_backup(tmp_path):
    """Regrese B02: další poškození zachová i předchozí zálohu a dovolí nový save."""
    path = tmp_path / "config.json"
    for payload in (b"{ first", b"{ second"):
        path.write_bytes(payload)
        cfg = c.load_config(path)
        c.save_config(cfg, path)
    assert sorted(p.read_bytes() for p in tmp_path.glob("config.json.corrupt-*")) == [b"{ first", b"{ second"]
    assert c.load_config(path).appearance == c.AppearanceConfig()


def test_wan_failed_backup_prevents_later_overwrite(tmp_path, monkeypatch, caplog):
    """Regrese B02: ani WAN update nesmí při selhání odložení přepsat původní cache."""
    path = tmp_path / "wan_state.json"
    payload = b"[]"
    path.write_bytes(payload)

    def deny_rename(*args, **kwargs):
        raise PermissionError("Test: nelze odložit")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", deny_rename)
        state = WanState(path)
        assert_defaults(state)
        state.update(WanResult("192.0.2.1", ts=1))
        assert path.read_bytes() == payload
        assert "Test: nelze odložit" in caplog.text
    # Nová instance po odstranění problému už může soubor odložit a uložit stav.
    state = WanState(path)
    assert_backup(path, payload)
    state.update(WanResult("192.0.2.1", ts=1))
    assert WanState(path).current == "192.0.2.1"


def test_config_huge_number_does_not_stop_loading(tmp_path):
    """Regrese B02: ani číslo mimo rozsah float nesmí uniknout z normalizace konfigurace."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"appearance": {"width": 10**400}}), encoding="utf-8")
    assert c.load_config(path).appearance.width == c.AppearanceConfig().width


def test_wan_valid_cache_is_kept_and_usable(tmp_path):
    """Regrese B02: validace neodstaví platnou perzistentní ISP cache."""
    import time

    path = tmp_path / "wan_state.json"
    s = WanState(path)
    s.update(WanResult("192.0.2.1", ts=1))
    s.update(WanResult("192.0.2.2", ts=2))
    s.isp_cache = {"192.0.2.2": ("Test ISP", time.time())}
    s.save()
    loaded = WanState(path)
    assert (loaded.previous, loaded.current, loaded.changed_at) == ("192.0.2.1", "192.0.2.2", 2)

    def no_fetch(*args):
        pytest.fail("Platná cache nesmí vyvolat síťový dotaz")

    assert loaded.isp_for("192.0.2.2", "ipinfo", 3600, no_fetch) == "Test ISP"
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("filename", ["config.json", "wan_state.json"])
@pytest.mark.parametrize("payload", [b"", b"{ broken", b'"invalid"', b"\xff\xfe"])
def test_controller_and_widget_start_with_corrupt_json(tmp_path, monkeypatch, filename, payload):
    """Regrese B02: konstrukce controlleru a GUI přežije poškození obou lokálních JSONů."""
    from PySide6.QtWidgets import QApplication
    from netmonitor.gui.controller import Controller
    from netmonitor.gui.main_widget import MainWidget
    from .fakes import FakePinger

    app = QApplication.instance() or QApplication([])
    app.setProperty("tray_available", False)
    path = tmp_path / filename
    path.write_bytes(payload)
    monkeypatch.setenv("NETMONITOR_HOME", str(tmp_path))
    monkeypatch.setattr("netmonitor.gui.controller.select_pinger", lambda: FakePinger({}))
    monkeypatch.setattr(Controller, "refresh_network", lambda self: None)
    monkeypatch.setattr(Controller, "refresh_wan", lambda self: None)
    cfg = c.load_config(tmp_path / "config.json")
    cfg.ping.targets = []
    ctrl = Controller(cfg, tmp_path / "config.json", tmp_path / "history.sqlite3")
    widget = None
    try:
        widget = MainWidget(ctrl)
        ctrl.start()
        widget.show()
        assert widget.isVisible() and ctrl.engine.running
        assert_backup(path, payload)
    finally:
        if widget is not None:
            widget.close()
        ctrl.shutdown()
