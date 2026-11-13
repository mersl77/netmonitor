"""Regrese lokálních profilů a převodu staré konfigurace."""
import json

import pytest

from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.core.profiles import ProfileStore


def test_existing_config_is_preserved_and_migrated(tmp_path):
    cfg = AppConfig.from_dict({})
    cfg.ping.targets = [PingTarget(name="Původní", host="10.0.0.138")]
    store = ProfileStore(tmp_path / "profiles")
    store.initialize(cfg)
    assert len(store.list_profiles()) == 1
    assert store.list_profiles()[0]["name"] == "Soukromý"
    assert store.load(store.active_id()).ping.targets[0].host == "10.0.0.138"
    assert not (tmp_path / "config.json").exists()


def test_create_switch_and_isolate_profiles(tmp_path):
    cfg = AppConfig.from_dict({})
    store = ProfileStore(tmp_path / "profiles")
    store.initialize(cfg)
    home = store.active_id()
    work_cfg = cfg.copy()
    work_cfg.ping.targets = [PingTarget(name="Firemní", host="172.16.0.1")]
    work = store.create("Firemní", work_cfg)
    assert store.active_id() == home
    assert store.activate(work).ping.targets[0].name == "Firemní"
    new_cfg = store.load(work)
    new_cfg.ping.targets[0].host = "172.16.0.2"
    store.save_active(new_cfg)
    assert store.load(home).ping.targets[0].host != "172.16.0.2"
    assert store.load(work).ping.targets[0].host == "172.16.0.2"


def test_cannot_delete_active_or_duplicate_names(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.initialize(AppConfig.from_dict({}))
    with pytest.raises(ValueError):
        store.delete(store.active_id())
    with pytest.raises(ValueError):
        store.create("souKRomý", AppConfig.from_dict({}))
    another = store.create("Zákazníci", AppConfig.from_dict({}))
    store.rename(another, "Firma")
    assert any(p["name"] == "Firma" for p in store.list_profiles())
    store.delete(another)
    assert len(store.list_profiles()) == 1


def test_corrupt_index_is_not_silently_overwritten(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    (root / "profile-index.json").write_text('{"profiles": [], "active": "bad"}', encoding="utf-8")
    store = ProfileStore(root)
    with pytest.raises(ValueError):
        store.initialize(AppConfig.from_dict({}))
    assert json.loads((root / "profile-index.json").read_text())["active"] == "bad"


def test_invalid_profile_id_cannot_escape_store(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.initialize(AppConfig.from_dict({}))
    with pytest.raises(ValueError):
        store.load("../config")
