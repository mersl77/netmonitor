from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import security
from app.main import create_app
from app.models import LicenseKey, User, now

from .conftest import PASSWORD, add_user, bearer, code, login, make_settings, new_key, register


def test_health_needs_no_login(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/profiles").status_code == 401
    assert client.get("/docs").status_code == 404


def test_registration_requires_valid_key(client, admin):
    assert register(client, "NM-AAAAA-AAAAA-AAAAA-AAAAA").json()["detail"]["code"] == "invalid_license_key"
    key = new_key(client, admin)
    r = register(client, key.lower())                      # klíč nezávisí na velikosti písmen
    assert r.status_code == 201 and r.json()["user"]["email"] == "user@example.org"
    assert r.json()["user"]["is_admin"] is False
    # jednorázový klíč podruhé nejde použít
    assert register(client, key, "druhy@example.org").status_code == 403
    assert login(client, "user@example.org").status_code == 200


def test_key_is_not_consumed_by_failed_registration(client, admin):
    key = new_key(client, admin)
    assert register(client, key, "neni-email").status_code == 422
    assert register(client, key, password="kratke").json()["detail"]["code"] == "weak_password"
    assert register(client, key, "admin@example.org").status_code == 409
    assert register(client, key).status_code == 201


def test_multi_use_expired_and_revoked_keys(app, client, admin):
    multi = new_key(client, admin, max_uses=2)
    assert register(client, multi, "a@example.org").status_code == 201
    assert register(client, multi, "b@example.org").status_code == 201
    assert register(client, multi, "c@example.org").status_code == 403

    expired = new_key(client, admin, valid_days=1)
    with app.state.session_factory() as db:
        obj = db.scalar(select(LicenseKey).where(LicenseKey.key_hash == security.sha256(expired)))
        obj.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert register(client, expired, "d@example.org").status_code == 403

    revoked = new_key(client, admin)
    key_id = [k for k in client.get("/admin/keys", headers=admin).json() if k["state"] == "active"][0]["id"]
    assert client.post(f"/admin/keys/{key_id}/revoke", headers=admin).json()["state"] == "revoked"
    assert register(client, revoked, "e@example.org").status_code == 403


def test_key_stored_only_as_hash_and_listed_with_note(app, client, admin):
    r = client.post("/admin/keys", json={"note": "Pavel – kancelář"}, headers=admin)
    key = r.json()["key"]
    assert key.startswith("NM-") and len(key) == 26
    register(client, key, "pavel@example.org")
    listed = client.get("/admin/keys", headers=admin).json()[0]
    assert "key" not in listed and listed["note"] == "Pavel – kancelář"
    assert listed["users"] == ["pavel@example.org"] and listed["state"] == "used"
    with app.state.session_factory() as db:
        assert key not in str(db.scalars(select(LicenseKey)).all()[0].__dict__)
    users = {u["email"]: u for u in client.get("/admin/users", headers=admin).json()}
    assert users["pavel@example.org"]["key_note"] == "Pavel – kancelář"


def test_registration_can_be_disabled():
    app = create_app(make_settings(registration_enabled=False))
    r = register(TestClient(app), "NM-AAAAA-AAAAA-AAAAA-AAAAA")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "registration_disabled"


def test_login_errors_do_not_reveal_accounts(app, client):
    add_user(app, "user@example.org")
    wrong = login(client, "user@example.org", password="spatne-heslo-123")
    missing = login(client, "nikdo@example.org")
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


def test_account_locks_after_repeated_failures(app, client):
    add_user(app, "user@example.org")
    for _ in range(5):
        assert login(client, "user@example.org", password="spatne-heslo-123").status_code == 401
    assert login(client, "user@example.org").json()["detail"]["code"] == "account_locked"


def test_ip_rate_limits():
    app = create_app(make_settings(rate_login=(3, 300), rate_register=(2, 3600)))
    c = TestClient(app)
    codes = [login(c, "x@example.org").status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    codes = [register(c, "NM-AAAAA-AAAAA-AAAAA-AAAAA").status_code for _ in range(3)]
    assert codes == [403, 403, 429]
    assert "Retry-After" in register(c, "x").headers


def test_general_rate_limit_and_body_limit():
    c = TestClient(create_app(make_settings(rate_general=(5, 60))))
    assert [c.get("/profiles").status_code for _ in range(6)][-1] == 429
    assert c.get("/health").status_code == 200
    c = TestClient(create_app(make_settings()))
    assert c.post("/auth/login", content=b"x" * (800 * 1024),
                  headers={"Content-Type": "application/json"}).status_code == 413


def test_refresh_rotation_and_reuse_detection(app, client):
    add_user(app, "user@example.org")
    first = login(client, "user@example.org").json()
    second = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert second.json()["refresh_token"] != first["refresh_token"]
    assert client.get("/auth/me", headers=bearer(second)).status_code == 200
    # starý token už neplatí a jeho použití ukončí celou relaci
    assert client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": second.json()["refresh_token"]}).status_code == 401
    assert client.get("/auth/me", headers=bearer(second)).status_code == 401


def test_logout_revokes_session(app, client):
    add_user(app, "user@example.org")
    r = login(client, "user@example.org")
    assert client.post("/auth/logout", json={}, headers=bearer(r)).status_code == 204
    assert client.get("/auth/me", headers=bearer(r)).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": r.json()["refresh_token"]}).status_code == 401


def test_password_change_logs_out_other_devices(app, client):
    add_user(app, "user@example.org")
    a, b = login(client, "user@example.org"), login(client, "user@example.org")
    body = {"current_password": PASSWORD, "new_password": "uplne-nove-heslo-1"}
    assert client.post("/auth/password", json={**body, "current_password": "spatne"}, headers=bearer(a)).status_code == 401
    assert client.post("/auth/password", json=body, headers=bearer(a)).status_code == 204
    assert client.get("/auth/me", headers=bearer(a)).status_code == 200
    assert client.get("/auth/me", headers=bearer(b)).status_code == 401
    assert login(client, "user@example.org").status_code == 401
    assert login(client, "user@example.org", password="uplne-nove-heslo-1").status_code == 200


def test_admin_requires_totp(app, client):
    add_user(app, admin=True)
    r = login(client)
    assert r.status_code == 200
    # správce bez 2FA nesmí vydávat klíče
    denied = client.post("/admin/keys", json={"note": "x"}, headers=bearer(r))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "totp_setup_required"
    setup = client.post("/auth/totp/setup", json={}, headers=bearer(r)).json()
    assert setup["uri"].startswith("otpauth://totp/NetMonitor:")
    assert client.post("/auth/totp/enable", json={"code": "000000"}, headers=bearer(r)).status_code == 401
    enabled = client.post("/auth/totp/enable", json={"code": code(setup["secret"])}, headers=bearer(r))
    assert enabled.status_code == 200 and enabled.json()["mfa"] is True
    assert client.post("/admin/keys", json={"note": "x"}, headers=bearer(r)).status_code == 201
    # další přihlášení už kód vyžaduje a stejný kód nejde použít znovu
    assert login(client).json()["detail"]["code"] == "totp_required"
    assert login(client, secret=setup["secret"]).json()["detail"]["code"] == "invalid_totp"
    with app.state.session_factory() as db:
        user = db.scalar(select(User))
        user.totp_last_step -= 5
        db.commit()
    assert login(client, secret=setup["secret"]).status_code == 200


def test_regular_user_cannot_use_admin_api(client, admin):
    key = new_key(client, admin)
    user = bearer(register(client, key))
    for method, path in (("get", "/admin/keys"), ("get", "/admin/users")):
        assert getattr(client, method)(path, headers=user).status_code == 403
    assert client.post("/admin/keys", json={"note": "x"}, headers=user).status_code == 403


def test_admin_can_disable_and_enable_user(client, admin):
    user = register(client, new_key(client, admin))
    uid = user.json()["user"]["id"]
    assert client.post(f"/admin/users/{uid}/disable", headers=admin).json()["is_active"] is False
    assert client.get("/profiles", headers=bearer(user)).status_code == 401
    assert login(client, "user@example.org").status_code == 401
    assert client.post(f"/admin/users/{uid}/enable", headers=admin).status_code == 200
    assert login(client, "user@example.org").status_code == 200
    me = client.get("/auth/me", headers=admin).json()["id"]
    assert client.post(f"/admin/users/{me}/disable", headers=admin).status_code == 409


def test_totp_matches_rfc6238_vector():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert security.totp_code(secret, 59 // 30) == "287082"
    assert security.totp_verify(secret, "287082", at=59) == 1
    assert security.totp_verify(secret, "287082", last_step=1, at=59) is None
