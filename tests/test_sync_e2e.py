"""Klient + synchronizační jádro proti skutečnému serveru: dva počítače, konflikty, licenční klíče."""
import pytest

from netmonitor.core import sync_engine as eng
from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.core.profiles import ProfileStore
from netmonitor.core.sync_client import SyncClient, SyncError

from .sync_server_fake import PASSWORD, URL, server  # noqa: F401


def cfg(host="10.0.0.1", name="Cíl") -> AppConfig:
    c = AppConfig.from_dict({})
    c.ping.targets = [PingTarget(name=name, host=host, id="t1")]
    return c


class PC:
    """Jeden počítač: vlastní místní profily a vlastní přihlášení."""

    def __init__(self, tmp_path, name, email="user@example.org", config=None):
        self.store = ProfileStore(tmp_path / name)
        self.store.initialize(config or cfg())
        self.client = SyncClient(URL)
        self.client.login(email, PASSWORD)
        self.account = email

    def sync(self, **resolutions) -> eng.Outcome:
        ids = {self.id(name): choice for name, choice in resolutions.items()}
        snap = eng.snapshot(self.store, self.account, URL)
        return eng.apply(self.store, snap, eng.run(self.client, snap, ids), cfg("0.0.0.0"))

    def id(self, name) -> str:
        return next(p["id"] for p in self.store.list_profiles() if p["name"] == name)

    def names(self) -> list[str]:
        return sorted(p["name"] for p in self.store.list_profiles())

    def host(self, name) -> str:
        return self.store.load(self.id(name)).ping.targets[0].host

    def edit(self, name, host) -> None:
        c = self.store.load(self.id(name))
        c.ping.targets[0].host = host
        self.store.save(self.id(name), c)


def test_profiles_travel_between_two_computers(server, tmp_path):
    server.add_user()
    home = PC(tmp_path, "home")
    home.store.create("Firemní", cfg("172.16.0.1"))
    out = home.sync()
    assert out.uploaded == 2 and not out.conflicts and not out.errors
    assert sorted(server.profiles()) == ["Firemní", "Soukromý"]

    work = PC(tmp_path, "work")                      # nový počítač se stejnou výchozí konfigurací
    out = work.sync()
    assert work.names() == ["Firemní", "Soukromý"] and out.downloaded == 1 and not out.conflicts
    assert work.host("Firemní") == "172.16.0.1"

    # další běh už nic nepřenáší
    server.requests.clear()
    assert (home.sync().uploaded, work.sync().uploaded) == (0, 0)
    assert all(method == "GET" and path == "/profiles" for method, path in server.requests)

    work.edit("Firemní", "172.16.0.99")
    assert work.sync().uploaded == 1
    out = home.sync()
    assert out.downloaded == 1 and home.host("Firemní") == "172.16.0.99"
    assert server.profiles()["Firemní"].revision == 2


def test_active_profile_change_from_server_is_reported(server, tmp_path):
    server.add_user()
    a, b = PC(tmp_path, "a"), PC(tmp_path, "b")
    a.sync(), b.sync()
    b.edit("Soukromý", "8.8.4.4")
    b.sync()
    out = a.sync()
    assert out.active_cfg is not None and out.active_cfg.ping.targets[0].host == "8.8.4.4"


def test_conflict_is_never_resolved_silently(server, tmp_path):
    server.add_user()
    a, b = PC(tmp_path, "a"), PC(tmp_path, "b")
    a.sync(), b.sync()
    a.edit("Soukromý", "1.1.1.1")
    b.edit("Soukromý", "2.2.2.2")
    a.sync()
    out = b.sync()
    assert [c.name for c in out.conflicts] == ["Soukromý"]
    assert b.host("Soukromý") == "2.2.2.2"
    assert server.profiles()["Soukromý"].config["ping"]["targets"][0]["host"] == "1.1.1.1"
    assert len(b.sync().conflicts) == 1                 # zůstává, dokud uživatel nerozhodne

    b.sync(**{"Soukromý": eng.KEEP_REMOTE})
    assert b.host("Soukromý") == "1.1.1.1" and not b.sync().conflicts


def test_conflict_keep_local_and_keep_both(server, tmp_path):
    server.add_user()
    a, b = PC(tmp_path, "a"), PC(tmp_path, "b")
    a.sync(), b.sync()
    a.edit("Soukromý", "1.1.1.1"), b.edit("Soukromý", "2.2.2.2")
    a.sync()
    b.sync(**{"Soukromý": eng.KEEP_LOCAL})
    a.sync()
    assert a.host("Soukromý") == "2.2.2.2"

    a.edit("Soukromý", "3.3.3.3"), b.edit("Soukromý", "4.4.4.4")
    a.sync()
    b.sync(**{"Soukromý": eng.KEEP_BOTH})
    assert b.names() == ["Soukromý", "Soukromý (místní kopie)"]
    assert b.host("Soukromý") == "3.3.3.3" and b.host("Soukromý (místní kopie)") == "4.4.4.4"
    b.sync()                                            # místní kopie se nahraje jako nový profil
    a.sync()
    assert a.names() == b.names()


def test_first_login_with_different_same_named_profile_asks(server, tmp_path):
    server.add_user()
    PC(tmp_path, "a", config=cfg("10.1.1.1")).sync()
    b = PC(tmp_path, "b", config=cfg("10.2.2.2"))
    out = b.sync()
    assert len(out.conflicts) == 1 and b.host("Soukromý") == "10.2.2.2"
    b.sync(**{"Soukromý": eng.KEEP_REMOTE})
    assert b.host("Soukromý") == "10.1.1.1"


def test_rename_and_delete_propagate(server, tmp_path):
    server.add_user()
    a, b = PC(tmp_path, "a"), PC(tmp_path, "b")
    a.store.create("Zákazník", cfg("192.168.5.1"))
    a.sync(), b.sync()
    a.store.rename(a.id("Zákazník"), "Zákazníci")
    a.sync()
    assert sorted(server.profiles()) == ["Soukromý", "Zákazníci"]
    b.sync()
    assert b.names() == ["Soukromý", "Zákazníci"]
    assert b.sync().uploaded == 0

    a.store.delete(a.id("Zákazníci"))
    server.offline = True                               # smazání počká na spojení
    with pytest.raises(SyncError):
        a.sync()
    server.offline = False
    a.sync()
    assert sorted(server.profiles()) == ["Soukromý"]
    assert a.sync().downloaded == 0 and a.names() == ["Soukromý"]
    # na druhém počítači profil zůstane jako místní a sám se znovu nenahraje
    b.sync()
    assert b.store.record(b.id("Zákazníci")).get("local_only") is True
    b.sync()
    assert sorted(server.profiles()) == ["Soukromý"]
    b.store.unbind(b.id("Zákazníci"))                   # „Znovu nahrát na server“
    b.sync()
    assert sorted(server.profiles()) == ["Soukromý", "Zákazníci"]


def test_profile_edited_during_sync_is_not_overwritten(server, tmp_path):
    server.add_user()
    a, b = PC(tmp_path, "a"), PC(tmp_path, "b")
    a.sync(), b.sync()
    a.edit("Soukromý", "1.1.1.1")
    a.sync()
    snap = eng.snapshot(b.store, b.account, URL)
    actions = eng.run(b.client, snap)
    b.edit("Soukromý", "9.9.9.9")                       # změna mezi stažením a zápisem
    eng.apply(b.store, snap, actions, cfg())
    assert b.host("Soukromý") == "9.9.9.9"
    assert len(b.sync().conflicts) == 1


def test_profiles_of_another_account_are_not_uploaded(server, tmp_path):
    server.add_user("a@example.org")
    server.add_user("b@example.org")
    pc = PC(tmp_path, "pc", "a@example.org")
    pc.sync()
    pc.client = SyncClient(URL)
    pc.client.login("b@example.org", PASSWORD)
    pc.account = "b@example.org"
    out = pc.sync()
    assert out.uploaded == 0 and server.profiles("b@example.org") == {}


def test_device_settings_stay_local(server, tmp_path):
    server.add_user()
    a = PC(tmp_path, "a")
    c = a.store.load(a.id("Soukromý"))
    c.appearance.x, c.network.adapter_name = 111, "Ethernet 5"
    c.network.ip_profiles = [{"name": "Kancelář", "dhcp": False, "ip": "10.20.30.2", "mask": "255.255.255.0",
                              "gateway": "10.20.30.1", "dns_dhcp": False, "dns": ["1.1.1.1"]}]
    a.store.save(a.id("Soukromý"), c)
    a.sync()
    stored = server.profiles()["Soukromý"].config
    assert set(stored) == {"ping", "notifications", "network"} and set(stored["network"]) == {"ip_profiles"}
    b = PC(tmp_path, "b")
    b.sync(**{"Soukromý": eng.KEEP_REMOTE})
    got = b.store.load(b.id("Soukromý"))
    assert got.network.ip_profiles[0]["ip"] == "10.20.30.2" and got.network.adapter_name != "Ethernet 5"


def test_license_key_registration_and_admin_flow(server):
    secret = server.add_admin()
    admin = SyncClient(URL)
    with pytest.raises(SyncError) as exc:
        admin.login("admin@example.org", PASSWORD)
    assert exc.value.code == "totp_required"
    admin.login("admin@example.org", PASSWORD, server.code(secret))
    assert admin.is_admin and admin.mfa

    key = admin.admin_create_key("Pavel – notebook", valid_days=30)
    assert key["key"].startswith("NM-") and key["note"] == "Pavel – notebook"

    user = SyncClient(URL)
    with pytest.raises(SyncError) as exc:
        user.register("pavel@example.org", PASSWORD, "NM-AAAAA-AAAAA-AAAAA-AAAAA")
    assert exc.value.code == "invalid_license_key"
    user.register("pavel@example.org", PASSWORD, key["key"])
    assert user.logged_in and not user.is_admin
    with pytest.raises(SyncError):
        SyncClient(URL).register("jiny@example.org", PASSWORD, key["key"])      # klíč je jednorázový
    with pytest.raises(SyncError) as exc:
        user.admin_keys()
    assert exc.value.status == 403

    listed = admin.admin_keys()[0]
    assert listed["users"] == ["pavel@example.org"] and listed["state"] == "used" and "key" not in listed
    row = next(u for u in admin.admin_users() if u["email"] == "pavel@example.org")
    assert row["key_note"] == "Pavel – notebook"
    admin.admin_set_user_active(row["id"], False)
    with pytest.raises(SyncError):
        user.profiles()
    assert not user.logged_in                                                    # relace zrušena serverem
    assert admin.admin_revoke_key(listed["id"])["state"] == "revoked"


def test_admin_enables_totp_from_client(server):
    server.add_user("admin@example.org", admin=True)
    admin = SyncClient(URL)
    admin.login("admin@example.org", PASSWORD)
    with pytest.raises(SyncError) as exc:
        admin.admin_keys()
    assert exc.value.code == "totp_setup_required"
    setup = admin.totp_setup()
    admin.totp_enable(server.code(setup["secret"]))
    assert admin.mfa and admin.user["totp_enabled"]
    assert admin.admin_keys() == []


def test_expired_access_token_is_refreshed_transparently(server):
    server.add_user()
    saved = []
    client = SyncClient(URL, on_tokens=saved.append)
    client.login("user@example.org", PASSWORD)
    first = client.refresh_token
    client.access_token = "neplatny.token.x"
    assert client.profiles() == []
    assert client.refresh_token != first and saved == [first, client.refresh_token]

    again = SyncClient(URL)                              # nový start aplikace
    again.resume(client.refresh_token)
    assert again.email == "user@example.org" and again.profiles() == []

    client.logout()
    assert not client.logged_in and saved[-1] is None
