import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security                      # noqa: E402
from app.main import create_app               # noqa: E402
from app.models import User                   # noqa: E402
from app.settings import Settings             # noqa: E402

PASSWORD = "spravne-dlouhe-heslo"
CONFIG = {"ping": {"targets": [{"name": "DNS", "host": "1.1.1.1"}]}, "notifications": {}}


def make_settings(**kw) -> Settings:
    base = dict(database_url="sqlite://", jwt_secret="x" * 48, rate_general=(10_000, 60),
                rate_login=(1000, 300), rate_register=(1000, 3600))
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def app():
    return create_app(make_settings())


@pytest.fixture
def client(app):
    return TestClient(app)


def add_user(app, email="admin@example.org", admin=False, totp=False) -> str | None:
    secret = security.new_totp_secret() if totp else None
    with app.state.session_factory() as db:
        db.add(User(email=email, password_hash=security.hash_password(PASSWORD), is_admin=admin,
                    totp_secret=secret, totp_enabled=totp))
        db.commit()
    return secret


def code(secret: str, offset: int = 0) -> str:
    return security.totp_code(secret, int(time.time() // 30) + offset)


def login(client, email="admin@example.org", secret=None, password=PASSWORD):
    body = {"email": email, "password": password}
    if secret:
        body["totp_code"] = code(secret)
    return client.post("/auth/login", json=body)


def bearer(response) -> dict:
    return {"Authorization": "Bearer " + response.json()["access_token"]}


@pytest.fixture
def admin(app, client):
    secret = add_user(app, admin=True, totp=True)
    return bearer(login(client, secret=secret))


def new_key(client, admin, **kw) -> str:
    r = client.post("/admin/keys", json={"note": "Test", **kw}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["key"]


def register(client, key, email="user@example.org", password=PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password, "license_key": key})
