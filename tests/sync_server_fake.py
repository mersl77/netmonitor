"""Skutečný sync server (FastAPI + SQLite v paměti) připojený ke klientovi bez sítě."""
import importlib
import json
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / "sync-server"
PASSWORD = "dlouhe-testovaci-heslo"
URL = "https://sync.test"


class _Response:
    def __init__(self, data: bytes):
        self.fp = BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.fp.close()

    def read(self, n=-1):
        return self.fp.read(n)


class Server:
    def __init__(self):
        pytest.importorskip("fastapi")
        pytest.importorskip("sqlalchemy")
        pytest.importorskip("argon2")
        pytest.importorskip("jwt")
        pytest.importorskip("httpx")
        if str(SERVER_DIR) not in sys.path:
            sys.path.insert(0, str(SERVER_DIR))
        from fastapi.testclient import TestClient
        main, settings = importlib.import_module("app.main"), importlib.import_module("app.settings")
        self.security, self.models = importlib.import_module("app.security"), importlib.import_module("app.models")
        self.app = main.create_app(settings.Settings(
            database_url="sqlite://", jwt_secret="t" * 48, rate_general=(100_000, 60),
            rate_login=(10_000, 300), rate_register=(10_000, 3600)))
        self.http = TestClient(self.app)
        self.offline = False
        self.requests: list[tuple[str, str]] = []

    def open(self, req, timeout=None):
        if self.offline:
            raise OSError("síť není dostupná")
        path = urlsplit(req.full_url).path
        self.requests.append((req.get_method(), path))
        r = self.http.request(req.get_method(), path, content=req.data, headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise HTTPError(req.full_url, r.status_code, "error", {}, BytesIO(r.content))
        return _Response(r.content)

    def add_admin(self, email="admin@example.org") -> str:
        """Vytvoří správce se zapnutým 2FA; vrací TOTP tajemství."""
        secret = self.security.new_totp_secret()
        with self.app.state.session_factory() as db:
            db.add(self.models.User(email=email, password_hash=self.security.hash_password(PASSWORD),
                                    is_admin=True, totp_secret=secret, totp_enabled=True))
            db.commit()
        return secret

    def add_user(self, email="user@example.org", admin=False) -> None:
        with self.app.state.session_factory() as db:
            db.add(self.models.User(email=email, password_hash=self.security.hash_password(PASSWORD),
                                    is_admin=admin))
            db.commit()

    def code(self, secret: str) -> str:
        return self.security.totp_code(secret, int(time.time() // 30))

    def profiles(self, email="user@example.org") -> dict:
        from sqlalchemy import select
        with self.app.state.session_factory() as db:
            user = db.scalar(select(self.models.User).where(self.models.User.email == email))
            rows = db.scalars(select(self.models.Profile).where(self.models.Profile.user_id == user.id)).all()
            return {p.name: p for p in rows}


@pytest.fixture
def server(monkeypatch):
    srv = Server()
    monkeypatch.setattr("netmonitor.core.sync_client._open", srv.open)
    return srv
