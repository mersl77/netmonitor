import json
import os
import sys

import pytest

from netmonitor.core.secret_store import SecretStore


def test_roundtrip_and_clear(tmp_path):
    store = SecretStore(tmp_path / "s.json")
    assert store.load() is None
    store.save("https://sync.zelina.eu", "a@example.org", "tajny-token-ěšč")
    assert store.load() == ("https://sync.zelina.eu", "a@example.org", "tajny-token-ěšč")
    store.clear()
    store.clear()
    assert store.load() is None


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SecretStore(path).load() is None
    path.write_text(json.dumps({"scheme": "file", "token": 5, "server": "x", "email": "y"}), encoding="utf-8")
    assert SecretStore(path).load() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX práva souboru")
def test_file_is_private_on_posix(tmp_path):
    store = SecretStore(tmp_path / "s.json")
    store.save("https://sync.zelina.eu", "a@example.org", "token")
    assert os.stat(store.path).st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI je jen ve Windows")
def test_token_is_encrypted_on_windows(tmp_path):
    store = SecretStore(tmp_path / "s.json")
    store.save("https://sync.zelina.eu", "a@example.org", "velmi-tajny-token")
    raw = store.path.read_text(encoding="utf-8")
    assert "velmi-tajny-token" not in raw and json.loads(raw)["scheme"] == "dpapi"
    assert store.load()[2] == "velmi-tajny-token"
