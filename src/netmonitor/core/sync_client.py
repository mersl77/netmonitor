"""HTTP klient self-hosted synchronizace NetMonitor.

Bez automatických retry na zápis: duplicitní změna nebo ztracená odpověď
nesmí potichu přepsat profil. Přístupový JWT se neukládá na disk.
"""
from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class SyncError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class SyncClient:
    base_url: str = "https://sync.zelina.eu"
    access_token: str | None = None
    timeout_s: float = 12.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Neplatná URL synchronizačního serveru")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        ):
            raise ValueError("Synchronizace vyžaduje HTTPS (kromě localhost)")
        if not parsed.hostname:
            raise ValueError("Chybí server")

    def _request(self, method: str, path: str, payload: dict | None = None):
        headers = {"Accept": "application/json", "User-Agent": "NetMonitor-Sync/1"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(body) > 512 * 1024:
                raise SyncError("Konfigurace přesahuje limit 512 KiB")
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        req = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout_s, context=ssl.create_default_context()) as resp:
                data = resp.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise SyncError("Odpověď serveru je příliš velká")
                return json.loads(data) if data else None
        except HTTPError as exc:
            if exc.code == 409:
                raise SyncError("Konflikt změn: stáhněte profil znovu.", 409) from exc
            if exc.code in (401, 403):
                raise SyncError("Přístup odmítnut. Přihlaste se znovu.", exc.code) from exc
            raise SyncError(f"Server vrátil HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"Server není dostupný: {exc}") from exc
        except (UnicodeError, ValueError) as exc:
            raise SyncError("Server vrátil neplatný JSON") from exc

    def login(self, email: str, password: str) -> None:
        result = self._request("POST", "/auth/login", {"email": email, "password": password})
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise SyncError("Server nevrátil access_token")
        self.access_token = token

    def logout(self) -> None:
        self.access_token = None

    def profiles(self) -> list[dict]:
        self._require_login()
        result = self._request("GET", "/profiles")
        profiles = result.get("profiles") if isinstance(result, dict) else result
        if not isinstance(profiles, list):
            raise SyncError("Neplatný seznam profilů")
        return profiles

    def download(self, profile_id: str) -> dict:
        self._require_login()
        from uuid import UUID
        pid = str(UUID(profile_id))
        result = self._request("GET", "/profiles/" + pid)
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise SyncError("Profil neobsahuje platnou konfiguraci")
        return result

    def upload(self, profile_id: str, config: dict, revision: int) -> dict:
        self._require_login()
        from uuid import UUID
        pid = str(UUID(profile_id))
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("Neplatné číslo revize")
        return self._request("PUT", "/profiles/" + pid,
                             {"config": config, "revision": revision})

    def create(self, name: str, config: dict) -> dict:
        self._require_login()
        if not name.strip() or len(name) > 100:
            raise ValueError("Neplatný název profilu")
        return self._request("POST", "/profiles", {"name": name.strip(), "config": config})

    def _require_login(self) -> None:
        if not self.access_token:
            raise SyncError("Nejprve se přihlaste")
