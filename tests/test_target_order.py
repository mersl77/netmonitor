from netmonitor.core import target_order as to
from netmonitor.core.config import AppConfig, PingTarget, new_section


def T(name):
    return PingTarget(name=name, host="1.1.1.1", id=name)


def S(name):
    return PingTarget(name=name, host="", kind="section", id=name)


def names(items):
    return [t.id for t in items]


def test_insert_after_selected_not_at_end():
    items = [T("a"), T("b"), T("c")]
    assert names(to.insert_target(items, T("x"), "a")) == ["a", "x", "b", "c"]
    assert names(to.insert_target(items, T("x"), None)) == ["a", "b", "c", "x"]
    sec = [S("DNS"), T("a"), S("WEB"), T("b")]
    assert names(to.insert_target(sec, T("x"), "WEB")) == ["DNS", "a", "WEB", "x", "b"]   # na začátek sekce


def test_move_target_crosses_sections():
    items = [S("DNS"), T("a"), S("WEB"), T("b")]
    out = to.move(items, "b", -1)
    assert names(out) == ["DNS", "a", "b", "WEB"]
    assert to.section_of(out, "b") == "DNS"
    assert names(to.move(items, "a", -1)) == ["a", "DNS", "WEB", "b"]       # nad první sekci = bez sekce
    assert names(to.move(items, "b", 1)) == names(items)                    # konec seznamu


def test_move_whole_section_block():
    items = [S("DNS"), T("a"), T("b"), S("WEB"), T("c"), S("LAN"), T("d")]
    assert names(to.move(items, "WEB", -1)) == ["WEB", "c", "DNS", "a", "b", "LAN", "d"]
    assert names(to.move(items, "DNS", 1)) == ["WEB", "c", "DNS", "a", "b", "LAN", "d"]
    assert names(to.move(items, "WEB", 1)) == ["DNS", "a", "b", "LAN", "d", "WEB", "c"]
    assert names(to.move(items, "LAN", 1)) == names(items)
    assert names(to.move(items, "DNS", -1)) == names(items)


def test_section_cannot_swallow_unsectioned_targets():
    items = [T("x"), S("DNS"), T("a")]
    assert names(to.move(items, "DNS", -1)) == names(items)


def test_remove_section_keeps_targets():
    items = [S("DNS"), T("a"), S("WEB"), T("b")]
    out = to.remove(items, "WEB")
    assert names(out) == ["DNS", "a", "b"]
    assert to.section_of(out, "b") == "DNS"


def test_reorder_by_drag_never_loses_items():
    items = [S("DNS"), T("a"), T("b"), S("WEB"), T("c")]
    assert names(to.reorder(items, ["WEB", "c", "DNS", "b", "a"])) == ["WEB", "c", "DNS", "b", "a"]
    assert names(to.reorder(items, ["c", "zzz"])) == ["c", "DNS", "a", "b", "WEB"]


def test_sections_in_config_roundtrip_and_not_monitored():
    cfg = AppConfig.from_dict({})
    cfg.ping.targets = [PingTarget(name=f"Cíl {i}", host=f"192.0.2.{i}") for i in range(1, 11)]
    cfg.ping.targets.insert(0, new_section("Domény"))
    cfg.ping.targets.insert(3, new_section("DNS"))
    data = cfg.to_dict()
    back = AppConfig.from_dict(data)
    assert [t.is_section for t in back.ping.targets][:4] == [True, False, False, True]
    assert back.ping.targets[0].name == "Domény" and back.ping.targets[0].host == ""
    assert len(back.real_targets()) == 10
    assert back.ping.targets[0].validate() == []


def test_engine_ignores_sections():
    from netmonitor.core.monitoring import MonitorEngine

    from .fakes import FakePinger
    eng = MonitorEngine(FakePinger())
    eng.set_targets([S("DNS"), T("a")])
    assert set(eng.snapshots()) == {"a"}
