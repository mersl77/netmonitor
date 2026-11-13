"""API klient: zabezpečený transport a ruční synchronizace.

Testy nekomunikují se skutečným serverem.
"""
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from netmonitor.core.sync_client import SyncClient, SyncError


class Response:
    def __init__(self, payload):
        self.fp = BytesIO(json.dumps(payload).encode())
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.fp.close()
    def read(self, n=-1):
        return self.fp.read(n)


def test_requires_https():
    with pytest.raises(ValueError):
        SyncClient("http://sync.zelina.eu")
    SyncClient("http://127.0.0.1:8766")


def test_reject_url_credentials():
    with pytest.raises(ValueError):
        SyncClient("https://admin:password@sync.zelina.eu")


def test_login_and_profiles(monkeypatch):
    calls = []
    def fake(req, **kwargs):
        calls.append((req.get_method(), req.full_url, req.headers))
        if req.full_url.endswith("/auth/login"):
            return Response({"access_token": "test-token"})
        if req.full_url.endswith("/profiles"):
            return Response([{"id": "11111111-1111-4111-8111-111111111111",
                              "name": "Soukromý", "revision": 1}])
        raise AssertionError(req.full_url)
    monkeypatch.setattr("netmonitor.core.sync_client._open", fake)
    client = SyncClient()
    client.login("me@example.org", "secret")
    assert client.access_token == "test-token"
    assert client.profiles()[0]["name"] == "Soukromý"
    assert calls[-1][2]["Authorization"] == "Bearer test-token"
    client.logout()
    assert client.access_token is None


def test_409_conflict(monkeypatch):
    def fake(*args, **kwargs):
        raise HTTPError("https://sync.zelina.eu/profiles/id", 409,
                        "Conflict", {}, None)
    monkeypatch.setattr("netmonitor.core.sync_client._open", fake)
    client = SyncClient(access_token="token")
    with pytest.raises(SyncError) as exc:
        client.upload("11111111-1111-4111-8111-111111111111", {}, 1)
    assert exc.value.status == 409


def test_require_auth_and_limit_payload(monkeypatch):
    client = SyncClient()
    with pytest.raises(SyncError):
        client.profiles()
    client.access_token = "token"
    with pytest.raises(SyncError, match="512 KiB"):
        client.create("Test", {"blob": "x" * 600000})
