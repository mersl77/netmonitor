from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import CONFIG, add_user, bearer, login, make_settings, new_key, register


def user(app, client, email="user@example.org"):
    add_user(app, email)
    return bearer(login(client, email))


def test_crud_and_revision_conflict(app, client):
    h = user(app, client)
    created = client.post("/profiles", json={"name": " Firemní ", "config": CONFIG}, headers=h).json()
    assert created["name"] == "Firemní" and created["revision"] == 1
    pid = created["id"]
    assert [p["name"] for p in client.get("/profiles", headers=h).json()] == ["Firemní"]
    assert "config" not in client.get("/profiles", headers=h).json()[0]
    changed = {**CONFIG, "notifications": {"enabled": True}}
    ok = client.put(f"/profiles/{pid}", json={"revision": 1, "config": changed}, headers=h)
    assert ok.json()["revision"] == 2
    stale = client.put(f"/profiles/{pid}", json={"revision": 1, "config": CONFIG}, headers=h)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict"
    assert client.get(f"/profiles/{pid}", headers=h).json()["config"] == changed
    assert client.delete(f"/profiles/{pid}", headers=h).status_code == 204
    assert client.get(f"/profiles/{pid}", headers=h).status_code == 404


def test_rename_and_unique_names(app, client):
    h = user(app, client)
    a = client.post("/profiles", json={"name": "Soukromý", "config": CONFIG}, headers=h).json()
    dup = client.post("/profiles", json={"name": "SOUKROMÝ", "config": CONFIG}, headers=h)
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "name_exists"
    client.post("/profiles", json={"name": "Firemní", "config": CONFIG}, headers=h)
    renamed = client.put(f"/profiles/{a['id']}", json={"revision": 1, "name": "Domácí"}, headers=h).json()
    assert renamed["name"] == "Domácí" and renamed["revision"] == 2 and renamed["config"] == CONFIG
    clash = client.put(f"/profiles/{a['id']}", json={"revision": 2, "name": "firemní"}, headers=h)
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "name_exists"


def test_profiles_are_isolated_between_accounts(app, client):
    a, b = user(app, client, "a@example.org"), user(app, client, "b@example.org")
    pid = client.post("/profiles", json={"name": "Soukromý", "config": CONFIG}, headers=a).json()["id"]
    assert client.get("/profiles", headers=b).json() == []
    assert client.get(f"/profiles/{pid}", headers=b).status_code == 404
    assert client.put(f"/profiles/{pid}", json={"revision": 1, "config": CONFIG}, headers=b).status_code == 404
    assert client.delete(f"/profiles/{pid}", headers=b).status_code == 404
    # stejný název u jiného účtu nevadí
    assert client.post("/profiles", json={"name": "Soukromý", "config": CONFIG}, headers=b).status_code == 201


def test_validation_and_limits():
    app = create_app(make_settings(max_profiles=2, max_config_bytes=2000))
    client = TestClient(app)
    h = user(app, client)
    assert client.post("/profiles", json={"name": "  ", "config": CONFIG}, headers=h).status_code == 422
    assert client.post("/profiles", json={"name": "A", "config": {"x": 1}}, headers=h).status_code == 422
    big = {"ping": {"pad": "x" * 3000}}
    assert client.post("/profiles", json={"name": "A", "config": big}, headers=h).status_code == 413
    assert client.post("/profiles", json={"name": "A", "config": CONFIG}, headers=h).status_code == 201
    assert client.post("/profiles", json={"name": "B", "config": CONFIG}, headers=h).status_code == 201
    over = client.post("/profiles", json={"name": "C", "config": CONFIG}, headers=h)
    assert over.status_code == 409 and over.json()["detail"]["code"] == "profile_limit"


def test_concurrent_update_same_revision_wins_once(tmp_path):
    app = create_app(make_settings(database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}"))
    client = TestClient(app)
    h = user(app, client)
    pid = client.post("/profiles", json={"name": "A", "config": CONFIG}, headers=h).json()["id"]

    def put(i):
        return client.put(f"/profiles/{pid}", json={"revision": 1, "config": {**CONFIG, "n": i}}, headers=h).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(put, range(8)))
    assert codes.count(200) == 1 and set(codes) <= {200, 409}


def test_full_flow_key_register_sync(client, admin):
    h = bearer(register(client, new_key(client, admin)))
    pid = client.post("/profiles", json={"name": "Práce", "config": CONFIG}, headers=h).json()["id"]
    assert client.get(f"/profiles/{pid}", headers=admin).status_code == 404   # ani správce nečte cizí profily
