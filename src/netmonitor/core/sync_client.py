"""HTTP klient synchronizačního serveru NetMonitor (účty, licenční klíče, profily).

Zápisy se nikdy neopakují automaticky: ztracená odpověď nesmí potichu přepsat profil.
Přístupový token žije jen v paměti; obnovovací token ukládá volající (SecretStore).
"""
from __future__ import annotations

import json
import ssl
import threading
from dataclasses import dataclass, field
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

DEFAULT_SERVER = "https://sync.zelina.eu"
MAX_CONFIG = 512 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    # Tokeny se nikdy nesmí přeposlat na jiný host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req, timeout: float):
    opener = build_opener(_RejectRedirects(), HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(req, timeout=timeout)


class SyncError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _device_name() -> str:
    import platform
    return f"{platform.node() or 'počítač'} ({platform.system()})"[:100]


@dataclass
class SyncClient:
    base_url: str = DEFAULT_SERVER
    access_token: str | None = None
    timeout_s: float = 12.0
    refresh_token: str | None = None
    user: dict | None = None
    mfa: bool = False
    on_tokens: Callable[[str | None], None] | None = None     # volá se při každé změně obnovovacího tokenu
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Neplatná URL synchronizačního serveru")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        ):
            raise ValueError("Synchronizace vyžaduje HTTPS (kromě localhost)")
        if not parsed.hostname:
            raise ValueError("Chybí server")

    # ------------------------------------------------------------------ transport
    def _send(self, method: str, path: str, payload: dict | None, auth: bool):
        headers = {"Accept": "application/json", "User-Agent": "NetMonitor-Sync/2"}
        body = None
        if payload is not None or method in ("POST", "PUT"):
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(body) > MAX_CONFIG + 4096:
                raise SyncError("Konfigurace přesahuje limit 512 KiB")
        if auth and self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        req = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with _open(req, timeout=self.timeout_s) as resp:
                data = resp.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise SyncError("Odpověď serveru je příliš velká")
                return json.loads(data) if data else None
        except HTTPError as exc:
            raise self._http_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"Server není dostupný: {getattr(exc, 'reason', exc)}") from exc
        except (UnicodeError, ValueError) as exc:
            raise SyncError("Server vrátil neplatnou odpověď") from exc

    @staticmethod
    def _http_error(exc: HTTPError) -> SyncError:
        code, message = "", ""
        try:
            detail = json.loads(exc.read(65536)).get("detail")
            if isinstance(detail, dict):
                code, message = str(detail.get("code", "")), str(detail.get("message", ""))
            elif isinstance(detail, str):
                message = detail
        except Exception:  # noqa: BLE001 – tělo chyby je nepovinné
            pass
        if not message:
            message = {401: "Přístup odmítnut. Přihlaste se znovu.", 403: "Přístup odmítnut.",
                       409: "Konflikt změn.", 429: "Příliš mnoho požadavků, zkuste to později."
                       }.get(exc.code, f"Server vrátil HTTP {exc.code}")
        return SyncError(message, exc.code, code)

    def _request(self, method: str, path: str, payload: dict | None = None):
        """Přihlášený požadavek. Po vypršení přístupového tokenu jej jednou obnoví a zopakuje.

        Opakování je bezpečné: odpověď 401 znamená, že server požadavek neprovedl.
        """
        with self._lock:
            if not self.access_token and self.refresh_token:
                self._refresh()
            if not self.access_token:
                raise SyncError("Nejprve se přihlaste", 401, "unauthorized")
            try:
                return self._send(method, path, payload, auth=True)
            except SyncError as exc:
                if exc.status != 401 or not self.refresh_token:
                    raise
            self._refresh()
            return self._send(method, path, payload, auth=True)

    def _accept(self, result) -> None:
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise SyncError("Server nevrátil access_token")
        self.access_token = token
        self.mfa = bool(result.get("mfa"))
        if isinstance(result.get("user"), dict):
            self.user = result["user"]
        refresh = result.get("refresh_token")
        if isinstance(refresh, str) and refresh and refresh != self.refresh_token:
            self.refresh_token = refresh
            if self.on_tokens:
                self.on_tokens(refresh)

    def _refresh(self) -> None:
        try:
            result = self._send("POST", "/auth/refresh", {"refresh_token": self.refresh_token}, auth=False)
        except SyncError as exc:
            if exc.status == 401:
                self._forget()
            raise
        self._accept(result)

    def _forget(self) -> None:
        self.access_token = self.refresh_token = self.user = None
        self.mfa = False
        if self.on_tokens:
            self.on_tokens(None)

    # ------------------------------------------------------------------ účet
    @property
    def logged_in(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    @property
    def email(self) -> str:
        return str((self.user or {}).get("email", ""))

    @property
    def is_admin(self) -> bool:
        return bool((self.user or {}).get("is_admin"))

    def server_info(self) -> dict:
        result = self._send("GET", "/health", None, auth=False)
        return result if isinstance(result, dict) else {}

    def login(self, email: str, password: str, totp_code: str | None = None) -> None:
        body = {"email": email, "password": password, "device": _device_name()}
        if totp_code:
            body["totp_code"] = totp_code
        with self._lock:
            self._accept(self._send("POST", "/auth/login", body, auth=False))

    def register(self, email: str, password: str, license_key: str) -> None:
        with self._lock:
            self._accept(self._send("POST", "/auth/register", {
                "email": email, "password": password, "license_key": license_key,
                "device": _device_name()}, auth=False))

    def resume(self, refresh_token: str) -> None:
        """Obnoví přihlášení z uloženého tokenu (po startu aplikace)."""
        with self._lock:
            self.refresh_token = refresh_token
            self._refresh()

    def logout(self) -> None:
        with self._lock:
            try:
                if self.access_token:
                    self._send("POST", "/auth/logout", {}, auth=True)
            except SyncError:
                pass                      # lokální odhlášení proběhne i bez spojení
            self._forget()

    def change_password(self, current: str, new: str) -> None:
        self._request("POST", "/auth/password", {"current_password": current, "new_password": new})

    def totp_setup(self) -> dict:
        return self._request("POST", "/auth/totp/setup", {})

    def totp_enable(self, code: str) -> None:
        with self._lock:
            self._accept(self._request("POST", "/auth/totp/enable", {"code": code}))

    # ------------------------------------------------------------------ profily
    def profiles(self) -> list[dict]:
        result = self._request("GET", "/profiles")
        profiles = result.get("profiles") if isinstance(result, dict) else result
        if not isinstance(profiles, list):
            raise SyncError("Neplatný seznam profilů")
        return [p for p in profiles if isinstance(p, dict)]

    def download(self, profile_id: str) -> dict:
        result = self._request("GET", "/profiles/" + str(UUID(profile_id)))
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise SyncError("Profil neobsahuje platnou konfiguraci")
        return result

    def upload(self, profile_id: str, config: dict | None, revision: int, name: str | None = None) -> dict:
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("Neplatné číslo revize")
        body: dict = {"revision": revision}
        if config is not None:
            body["config"] = config
        if name is not None:
            body["name"] = name
        return self._check_profile(self._request("PUT", "/profiles/" + str(UUID(profile_id)), body))

    def create(self, name: str, config: dict) -> dict:
        if not name.strip() or len(name) > 100:
            raise ValueError("Neplatný název profilu")
        return self._check_profile(self._request("POST", "/profiles", {"name": name.strip(), "config": config}))

    def delete(self, profile_id: str) -> None:
        self._request("DELETE", "/profiles/" + str(UUID(profile_id)))

    @staticmethod
    def _check_profile(result) -> dict:
        if (not isinstance(result, dict) or not isinstance(result.get("revision"), int)
                or result["revision"] < 1):
            raise SyncError("Server vrátil neplatný profil")
        UUID(str(result.get("id")))
        return result

    # ------------------------------------------------------------------ správa (jen správce s 2FA)
    def admin_keys(self) -> list[dict]:
        return self._request("GET", "/admin/keys")

    def admin_create_key(self, note: str, max_uses: int = 1, valid_days: int | None = None) -> dict:
        body: dict = {"note": note, "max_uses": max_uses}
        if valid_days:
            body["valid_days"] = valid_days
        return self._request("POST", "/admin/keys", body)

    def admin_revoke_key(self, key_id: str) -> dict:
        return self._request("POST", f"/admin/keys/{UUID(key_id)}/revoke", {})

    def admin_users(self) -> list[dict]:
        return self._request("GET", "/admin/users")

    def admin_set_user_active(self, user_id: str, active: bool) -> dict:
        return self._request("POST", f"/admin/users/{UUID(user_id)}/{'enable' if active else 'disable'}", {})
