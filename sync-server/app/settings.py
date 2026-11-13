"""Nastavení serveru z proměnných prostředí (Portainer → Stack → Environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _secret(name: str) -> str:
    """Hodnota z proměnné NAME, případně ze souboru NAME_FILE (Docker secret)."""
    value = os.environ.get(name, "").strip()
    path = os.environ.get(name + "_FILE", "").strip()
    if not value and path:
        value = Path(path).read_text(encoding="utf-8").strip()
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    access_minutes: int = 15
    refresh_days: int = 60
    registration_enabled: bool = True
    max_profiles: int = 20
    max_config_bytes: int = 512 * 1024
    max_sessions: int = 10
    # ochrana proti zneužití (počet / okno v sekundách na jednu IP adresu)
    rate_general: tuple[int, int] = (240, 60)
    rate_login: tuple[int, int] = (10, 300)
    rate_register: tuple[int, int] = (5, 3600)
    lock_after_failures: int = 5
    lock_minutes: int = 15


def load() -> Settings:
    secret = _secret("JWT_SECRET")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET musí mít alespoň 32 znaků (např. `openssl rand -hex 32`).")
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        password = _secret("POSTGRES_PASSWORD")
        if not password:
            raise RuntimeError("Chybí DATABASE_URL nebo POSTGRES_PASSWORD.")
        from urllib.parse import quote
        url = "postgresql+psycopg://{u}:{p}@{h}:{port}/{d}".format(
            u=quote(os.environ.get("POSTGRES_USER", "netmonitor"), safe=""), p=quote(password, safe=""),
            h=os.environ.get("POSTGRES_HOST", "db"), port=_int("POSTGRES_PORT", 5432),
            d=quote(os.environ.get("POSTGRES_DB", "netmonitor"), safe=""))
    return Settings(
        database_url=url, jwt_secret=secret,
        access_minutes=_int("ACCESS_TOKEN_MINUTES", 15),
        refresh_days=_int("REFRESH_TOKEN_DAYS", 60),
        registration_enabled=os.environ.get("REGISTRATION_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"),
        max_profiles=_int("MAX_PROFILES_PER_USER", 20),
        rate_general=(_int("RATE_GENERAL_PER_MINUTE", 240), 60),
        rate_login=(_int("RATE_LOGIN_PER_5MIN", 10), 300),
        rate_register=(_int("RATE_REGISTER_PER_HOUR", 5), 3600),
    )
