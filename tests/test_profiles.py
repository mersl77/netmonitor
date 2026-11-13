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

def test_rename_keeps_server_binding_and_delete_is_queued(tmp_path):
    from uuid import uuid4
    store = ProfileStore(tmp_path / 'profiles')
    store.initialize(AppConfig.from_dict({}))
    remote = str(uuid4())
    pid = store.create('Firma', AppConfig.from_dict({}))
    store.bind_remote(pid, remote, 1, 'Test@Example.org', 'https://sync.zelina.eu/', 'abc')
    store.rename(pid, 'Práce')
    rec = store.record(pid)
    assert rec['name'] == 'Práce' and rec['remote_id'] == remote and rec['synced_hash'] == 'abc'
    assert rec['remote_account'] == 'test@example.org' and rec['remote_server'] == 'https://sync.zelina.eu'
    store.delete(pid)
    assert store.pending_deletes('test@example.org', 'https://sync.zelina.eu') == [remote]
    assert store.pending_deletes('jiny@example.org', 'https://sync.zelina.eu') == []
    store.clear_pending_delete(remote)
    assert store.pending_deletes('test@example.org', 'https://sync.zelina.eu') == []


def test_unbind_local_only_and_unique_name(tmp_path):
    from uuid import uuid4
    store = ProfileStore(tmp_path / 'profiles')
    store.initialize(AppConfig.from_dict({}))
    pid = store.active_id()
    store.bind_remote(pid, str(uuid4()), 3, 'a@example.org', 'https://sync.zelina.eu', 'h')
    store.unbind(pid, local_only=True)
    assert store.record(pid) == {'id': pid, 'name': 'Soukromý', 'local_only': True}
    store.unbind(pid)
    assert 'local_only' not in store.record(pid)
    assert store.unique_name('soukromý') == 'soukromý (2)'
    assert store.unique_name('Soukromý', skip_id=pid) == 'Soukromý'
