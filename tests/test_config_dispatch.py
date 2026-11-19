"""Regrese G01/B06: změny konfigurace zasahují jen příslušnou část aplikace."""
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PySide6")

from netmonitor.core.config import AppConfig, PingTarget  # noqa: E402
from netmonitor.gui.controller import Controller  # noqa: E402
from netmonitor.gui.main_widget import MainWidget  # noqa: E402
from netmonitor.gui.settings_dialog import SettingsDialog  # noqa: E402

from .fakes import FakePinger  # noqa: E402
from .test_gui_smoke import pump, qapp  # noqa: E402, F401


@pytest.fixture
def controller(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("netmonitor.gui.controller.select_pinger", FakePinger)
    cfg = AppConfig()
    cfg.network.external_api = False
    cfg.ping.targets = [PingTarget(name="Test", host="192.0.2.1", id="test")]
    c = Controller(cfg, tmp_path / "config.json", tmp_path / "history.sqlite3")
    monkeypatch.setattr(c.net_provider, "list_interfaces", lambda: [])
    # Deterministické zápisy bez vláken; plánovač ani síťové časovače nespouštíme.
    monkeypatch.setattr(c, "_run_bg", lambda key, fn, done=None: fn())
    c._apply_targets()
    c._eval_timer.stop()
    yield c
    c.shutdown()


def test_opacity_does_not_reapply_targets_or_history(controller):
    """Regrese G01: průhlednost nemění engine, DB, cílové řádky ani evaluaci."""
    w = MainWidget(controller)
    try:
        with (patch.object(controller.engine, "set_targets") as targets,
              patch.object(controller.history, "upsert_targets") as upsert,
              patch.object(controller.notifier, "update_config") as notifier,
              patch.object(w.ping, "set_targets") as rows):
            cfg = controller.cfg.copy()
            cfg.appearance.opacity = 0.75
            controller.update_config(cfg, save=False)
            targets.assert_not_called()
            upsert.assert_not_called()
            notifier.assert_not_called()
            rows.assert_not_called()
            assert not controller._eval_timer.isActive()
            assert w.windowOpacity() == pytest.approx(0.75, abs=0.01)
    finally:
        w.close()


@pytest.mark.parametrize("slider", ["opacity", "glass", "both"])
def test_slider_burst_has_one_update_and_restyle(controller, slider):
    """Regrese G01: dvacet rychlých změn se sloučí v jeden update a restyle."""
    w = MainWidget(controller)
    d = SettingsDialog(controller, w)
    try:
        with (patch.object(controller, "update_config", wraps=controller.update_config) as update,
              patch.object(w.ping, "apply_style", wraps=w.ping.apply_style) as restyle):
            for value in range(1, 21):
                if slider in ("opacity", "both"):
                    d.opacity.setValue(value)
                if slider in ("glass", "both"):
                    d.glass.setValue(value)
            update.assert_not_called()
            pump(0.2)
            assert update.call_count == restyle.call_count == 1
            assert controller.cfg.appearance.opacity == (0.8 if slider != "glass" else 1.0)
            assert controller.cfg.appearance.glass_alpha == (0.8 if slider != "opacity" else 1.0)
    finally:
        d.close()
        w.close()


@pytest.mark.parametrize("finish", ["close", "reject", "apply", "external"])
def test_pending_sliders_survive_close_or_other_changes(controller, finish):
    """Regrese G01: poslední hodnota se neztratí zavřením ani jinou změnou."""
    w = MainWidget(controller)
    d = SettingsDialog(controller, w)
    d.show()
    try:
        d.opacity.setValue(27)
        if finish in ("close", "reject"):
            getattr(d, finish)()
        elif finish == "apply":
            d._apply(lambda cfg: setattr(cfg.appearance, "compact", True))
        else:
            cfg = controller.cfg.copy()
            cfg.notifications.enabled = False
            controller.update_config(cfg, save=False)
        pump(0.2)
        assert controller.cfg.appearance.opacity == 0.73
        if finish == "apply":
            assert controller.cfg.appearance.compact
        if finish == "external":
            assert not controller.cfg.notifications.enabled
            assert d.opacity.value() == 27
    finally:
        d.close()
        w.close()


def test_limit_and_thresholds_do_not_rebuild_targets(controller):
    """Regrese B06/G01: limit se předá enginu; prahy jen obnoví styl a evaluaci."""
    w = MainWidget(controller)
    try:
        with (patch.object(controller.engine, "set_targets") as targets,
              patch.object(controller.history, "upsert_targets") as upsert,
              patch.object(w.ping, "apply_style", wraps=w.ping.apply_style) as restyle):
            cfg = controller.cfg.copy()
            cfg.ping.max_concurrent = 2
            controller.update_config(cfg, save=False)
            assert controller.engine.max_concurrent == 2
            restyle.assert_not_called()
            cfg = controller.cfg.copy()
            cfg.ping.thresholds.orange_max_ms = 200
            controller.update_config(cfg, save=False)
            assert restyle.call_count == 1
            assert controller._eval_timer.isActive()
            targets.assert_not_called()
            upsert.assert_not_called()
    finally:
        w.close()


def test_changed_target_and_gateway_still_dispatch(controller):
    """Regrese G01: skutečná změna cíle či gateway_probe dál obnoví monitoring."""
    changed = Mock()
    controller.targets_changed.connect(changed)
    with (patch.object(controller.engine, "set_targets", wraps=controller.engine.set_targets) as targets,
          patch.object(controller.history, "upsert_targets", wraps=controller.history.upsert_targets) as upsert):
        cfg = controller.cfg.copy()
        cfg.ping.targets[0].host = "192.0.2.2"
        controller.update_config(cfg, save=False)
        assert targets.call_count == upsert.call_count == changed.call_count == 1
        assert controller.engine.snapshot("test").host == "192.0.2.2"
        cfg = controller.cfg.copy()
        cfg.network.gateway_probe = False
        controller.update_config(cfg, save=False)
        assert targets.call_count == 2


def test_checked_glass_menu_action_stays_checked_on_noop(controller):
    """Regrese N1: opakovaná volba stejného krytí obnoví zaškrtnutí tray menu."""
    from PySide6.QtWidgets import QMenu

    from netmonitor.gui.tray import Tray

    w = MainWidget(controller)
    tray = Tray(w)
    changed = Mock()
    controller.config_changed.connect(changed)

    def glass_actions():
        menu = tray.contextMenu()
        assert menu is not None
        submenu = next(a.menu() for a in menu.actions() if a.text() == "Průhlednost pozadí")
        assert isinstance(submenu, QMenu)
        return submenu.actions()

    try:
        selected = next(a for a in glass_actions() if a.isChecked())
        text = selected.text()
        with (patch.object(controller.engine, "set_targets") as targets,
              patch.object(controller.history, "upsert_targets") as upsert):
            selected.trigger()
            assert next(a for a in glass_actions() if a.text() == text).isChecked()
            assert changed.call_count == 1
            assert not controller._save_timer.isActive()
            targets.assert_not_called()
            upsert.assert_not_called()
    finally:
        tray.deleteLater()
        w.close()


def test_section_heading_change_only_updates_target_rows(controller):
    """Regrese G01: samotný nadpis sekce vyšle signál bez zásahu do enginu či DB."""
    from netmonitor.core.config import new_section

    cfg = controller.cfg.copy()
    cfg.ping.targets.insert(0, new_section("Původní nadpis"))
    controller.update_config(cfg, save=False)
    w = MainWidget(controller)
    changed = Mock()
    controller.targets_changed.connect(changed)
    try:
        with (patch.object(controller.engine, "set_targets") as targets,
              patch.object(controller.history, "upsert_targets") as upsert):
            cfg = controller.cfg.copy()
            cfg.ping.targets[0].name = "Nový nadpis"
            controller.update_config(cfg, save=False)
            assert changed.call_count == 1
            assert w.ping.sections[cfg.ping.targets[0].id].target.name == "Nový nadpis"
            targets.assert_not_called()
            upsert.assert_not_called()
    finally:
        w.close()


@pytest.mark.parametrize("slider", ["opacity", "glass"])
def test_slider_updates_during_continuous_drag(controller, slider):
    """Regrese N3: souvislé tažení průběžně mění vzhled už před uvolněním."""
    from PySide6.QtCore import QEventLoop, QTimer

    w = MainWidget(controller)
    d = SettingsDialog(controller, w)
    d.show()
    loop = QEventLoop()
    drag = QTimer()
    ticks = 0
    during_drag = []

    def move_slider():
        nonlocal ticks
        ticks += 1
        getattr(d, slider).setValue(ticks)

    drag.timeout.connect(move_slider)
    key = "opacity" if slider == "opacity" else "glass_alpha"
    controller.config_changed.connect(lambda cfg: during_drag.append((drag.isActive(), getattr(cfg.appearance, key))))
    try:
        drag.start(10)
        QTimer.singleShot(250, loop.quit)
        loop.exec()
        assert ticks >= 10
        assert any(active and value < 1.0 for active, value in during_drag)
    finally:
        drag.stop()
        d.close()
        assert getattr(controller.cfg.appearance, key) == pytest.approx(1 - ticks / 100)
        w.close()
