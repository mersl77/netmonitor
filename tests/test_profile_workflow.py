"""Desktop profile workflows with isolated files and an in-memory cloud."""
from uuid import uuid4

from PySide6.QtWidgets import QMessageBox

from netmonitor.core.config import AppConfig, PingTarget, load_config
from netmonitor.core.profiles import ProfileStore
from netmonitor.gui.settings_dialog import SettingsDialog
from .test_gui_smoke import ctrl, qapp, pump


def dialog(ctrl, tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    store.initialize(ctrl.cfg)
    ctrl.profile_store = store
    from netmonitor.gui.main_widget import MainWidget
    return SettingsDialog(ctrl, MainWidget(ctrl)), store


def test_create_edit_switch_delete_preserves_dns_and_restart(ctrl, tmp_path, monkeypatch):
    d, store = dialog(ctrl, tmp_path)
    home = store.active_id()
    monkeypatch.setattr('netmonitor.gui.settings_dialog.QInputDialog.getText',
                        lambda *a, **k: ('Pracovní', True))
    d._profile_create()
    work = store.active_id()
    assert work != home
    assert d.profile_selector.currentText() == 'Pracovní'
    cfg = ctrl.cfg.copy()
    cfg.ping.targets = [PingTarget('Firma', '10.20.30.1')]
    cfg.network.ip_profiles = [{'name': 'Firma', 'dhcp': False, 'ip': '10.20.30.2',
        'mask': '255.255.255.0', 'gateway': '10.20.30.1', 'dns_dhcp': False, 'dns': ['1.1.1.1']}]
    ctrl.update_config(cfg)
    d.profile_selector.setCurrentIndex(d.profile_selector.findData(home))
    d._profile_activate()
    assert ctrl.cfg.ping.targets[0].name == 'Zelina'
    assert store.load(work).network.ip_profiles[0]['dns'] == ['1.1.1.1']
    d.profile_selector.setCurrentIndex(d.profile_selector.findData(work))
    d._profile_activate()
    assert ctrl.cfg.ping.targets[0].name == 'Firma'
    assert load_config(ctrl.config_path).network.ip_profiles == cfg.network.ip_profiles
    assert ProfileStore(store.root).active_id() == work
    d.profile_selector.setCurrentIndex(d.profile_selector.findData(home))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    d._profile_delete()
    assert store.active_id() == work
    assert len(store.list_profiles()) == 1
    d.close()


def test_upload_selected_not_active_download_and_delete(ctrl, tmp_path, monkeypatch):
    d, store = dialog(ctrl, tmp_path)
    active = store.active_id()
    cfg = AppConfig.from_dict({})
    cfg.ping.targets = [PingTarget('Zákazník', '10.10.10.10')]
    selected = store.create('Zákazník', cfg)
    d._profiles_refresh(selected)
    d._sync_account = 'test@example.org'
    cloud = {}
    def create(name, config):
        pid = str(uuid4())
        cloud[pid] = {'id': pid, 'name': name, 'config': config, 'revision': 1}
        return cloud[pid].copy()
    def upload(pid, config, revision):
        assert cloud[pid]['revision'] == revision
        cloud[pid].update(config=config, revision=revision+1)
        return cloud[pid].copy()
    monkeypatch.setattr(d.sync_client, 'create', create)
    monkeypatch.setattr(d.sync_client, 'upload', upload)
    monkeypatch.setattr(d.sync_client, 'profiles', lambda: list(cloud.values()))
    monkeypatch.setattr(d.sync_client, 'download', lambda pid: cloud[pid].copy())
    monkeypatch.setattr(d.sync_client, 'delete', lambda pid: cloud.pop(pid))
    # Synchronous network executor keeps this workflow test deterministic.
    monkeypatch.setattr(ctrl, '_run_bg', lambda key, fn, done=None: done(fn()) if done else None)
    d._sync_upload()
    remote = next(iter(cloud.values()))
    assert remote['name'] == 'Zákazník'
    assert remote['config']['ping']['targets'][0]['host'] == '10.10.10.10'
    assert store.active_id() == active
    assert 'remote_id' not in store.record(active)
    d._sync_upload()
    assert len(cloud) == 1 and remote['revision'] == 2
    d._sync_download()
    downloaded = d.profile_selector.currentData()
    assert downloaded not in (active, selected)
    assert store.record(downloaded)['name'] == 'Zákazník (2)'
    assert store.active_id() == active
    assert store.load(downloaded).ping.targets[0].host == '10.10.10.10'
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    d._sync_delete()
    assert not cloud and len(store.list_profiles()) == 3
    assert 'remote_id' not in store.record(selected)
    assert 'remote_id' not in store.record(downloaded)
    d.close()


def test_sync_failure_and_busy_preserve_profiles(ctrl, tmp_path, monkeypatch):
    d, store = dialog(ctrl, tmp_path)
    before = store.load(store.active_id()).to_dict()
    calls = []
    monkeypatch.setattr(ctrl, '_run_bg', lambda *a: calls.append(a))
    d._sync_refresh()
    d._sync_refresh()
    assert len(calls) == 1 and not d.tabs.isEnabled()
    d._sync_done('refresh', (False, 'HTTP 401'))
    assert d.tabs.isEnabled() and '401' in d.sync_status.text()
    assert store.load(store.active_id()).to_dict() == before
    d.close()

def test_invalid_download_leaves_store_untouched(ctrl, tmp_path):
    d, store = dialog(ctrl, tmp_path)
    before = store.list_profiles()
    d._sync_done('download', (True, ({'id': str(uuid4()), 'name': 'Broken',
        'revision': 0, 'config': {'ping': {}}}, ctrl.cfg.copy(), 'a', 'https://sync.zelina.eu')))
    assert store.list_profiles() == before
    assert 'Neplatná revize' in d.sync_status.text()
    d.close()
