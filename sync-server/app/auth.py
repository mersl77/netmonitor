"""Registrace licenčním klíčem, přihlášení, rotace obnovovacích tokenů a dvoufázové ověření."""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import security
from .deps import Auth, current_auth, error, get_db, limit
from .models import LicenseKey, User, UserSession, aware, now

router = APIRouter(prefix="/auth", tags=["auth"])
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MIN_PASSWORD = 10


class RegisterBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    license_key: str = Field(max_length=64)
    device: str = Field(default="", max_length=100)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    totp_code: str | None = Field(default=None, max_length=12)
    device: str = Field(default="", max_length=100)


class RefreshBody(BaseModel):
    refresh_token: str = Field(max_length=200)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class CodeBody(BaseModel):
    code: str = Field(max_length=12)


def normalize_email(raw: str) -> str:
    email = raw.strip().casefold()
    if len(email) > 254 or not EMAIL.fullmatch(email):
        raise error(422, "invalid_email", "Neplatná e-mailová adresa.")
    return email


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise error(422, "weak_password", f"Heslo musí mít alespoň {MIN_PASSWORD} znaků.")


def user_dict(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "is_admin": user.is_admin,
            "totp_enabled": user.totp_enabled}


def open_session(request: Request, db: Session, user: User, device: str, mfa: bool) -> dict:
    settings = request.app.state.settings
    # Omezení počtu zařízení: nejstarší relace se ukončí.
    active = db.scalars(select(UserSession).where(
        UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
    ).order_by(UserSession.last_used_at.desc())).all()
    for old in active[settings.max_sessions - 1:]:
        old.revoked_at = now()
    token = security.new_refresh_token()
    session = UserSession(user_id=user.id, token_hash=security.sha256(token), device=device.strip()[:100],
                          mfa=mfa, expires_at=now() + timedelta(days=settings.refresh_days))
    db.add(session)
    user.last_login_at = now()
    db.commit()
    return token_response(request, user, session, token)


def token_response(request: Request, user: User, session: UserSession, refresh_token: str) -> dict:
    settings = request.app.state.settings
    return {
        "access_token": security.make_access_token(settings.jwt_secret, user.id, session.id, session.mfa,
                                                   settings.access_minutes),
        "refresh_token": refresh_token, "token_type": "bearer",
        "expires_in": settings.access_minutes * 60, "mfa": bool(session.mfa), "user": user_dict(user),
    }


@router.post("/register", status_code=201)
def register(body: RegisterBody, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    limit(request, "register", settings.rate_register)
    if not settings.registration_enabled:
        raise error(403, "registration_disabled", "Registrace je na tomto serveru vypnutá.")
    email = normalize_email(body.email)
    check_password(body.password)
    key = db.scalar(select(LicenseKey).where(
        LicenseKey.key_hash == security.sha256(security.normalize_license_key(body.license_key))))
    if (key is None or key.revoked_at is not None or key.uses >= key.max_uses
            or (key.expires_at is not None and aware(key.expires_at) <= now())):
        raise error(403, "invalid_license_key", "Licenční klíč je neplatný, vyčerpaný nebo mu skončila platnost.")
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise error(409, "email_exists", "Účet s tímto e-mailem už existuje.")
    # Atomické čerpání klíče: dva souběžné požadavky nesmí jeden klíč použít dvakrát.
    taken = db.execute(update(LicenseKey).where(
        LicenseKey.id == key.id, LicenseKey.uses < LicenseKey.max_uses, LicenseKey.revoked_at.is_(None)
    ).values(uses=LicenseKey.uses + 1))
    if taken.rowcount != 1:
        db.rollback()
        raise error(403, "invalid_license_key", "Licenční klíč je neplatný, vyčerpaný nebo mu skončila platnost.")
    user = User(email=email, password_hash=security.hash_password(body.password), license_key_id=key.id)
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise error(409, "email_exists", "Účet s tímto e-mailem už existuje.") from exc
    return open_session(request, db, user, body.device, mfa=False)


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    limit(request, "login", settings.rate_login)
    email = body.email.strip().casefold()
    user = db.scalar(select(User).where(User.email == email))
    bad = error(401, "invalid_credentials", "Nesprávný e-mail nebo heslo.")
    if user is not None and user.locked_until is not None and aware(user.locked_until) > now():
        security.verify_password(None, body.password)        # stejná doba odezvy
        raise error(429, "account_locked", "Účet je dočasně zamčen po opakovaných chybných pokusech.")
    ok = security.verify_password(user.password_hash if user else None, body.password)
    if user is None:
        raise bad
    if not ok or not user.is_active:
        fail(db, user, settings)
        raise bad
    mfa = False
    if user.totp_enabled:
        if not body.totp_code:
            raise error(401, "totp_required", "Zadejte kód z autentikátoru.")
        step = security.totp_verify(user.totp_secret, body.totp_code, user.totp_last_step)
        if step is None:
            fail(db, user, settings)
            raise error(401, "invalid_totp", "Nesprávný nebo již použitý kód z autentikátoru.")
        user.totp_last_step = step
        mfa = True
    user.failed_logins = 0
    user.locked_until = None
    return open_session(request, db, user, body.device, mfa=mfa)


def fail(db: Session, user: User, settings) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.lock_after_failures:
        user.locked_until = now() + timedelta(minutes=settings.lock_minutes)
        user.failed_logins = 0
    db.commit()


@router.post("/refresh")
def refresh(body: RefreshBody, request: Request, db: Session = Depends(get_db)):
    limit(request, "refresh", (60, 300))
    digest = security.sha256(body.refresh_token)
    session = db.scalar(select(UserSession).where(UserSession.token_hash == digest))
    if session is None:
        # Použití už vyměněného tokenu = token byl nejspíš zkopírován. Ukončí se celá relace.
        stolen = db.scalar(select(UserSession).where(UserSession.previous_hash == digest))
        if stolen is not None and stolen.revoked_at is None:
            stolen.revoked_at = now()
            db.commit()
        raise error(401, "unauthorized", "Relace vypršela. Přihlaste se znovu.")
    user = db.get(User, session.user_id)
    if (session.revoked_at is not None or aware(session.expires_at) <= now()
            or user is None or not user.is_active):
        raise error(401, "unauthorized", "Relace vypršela. Přihlaste se znovu.")
    token = security.new_refresh_token()
    session.previous_hash = session.token_hash
    session.token_hash = security.sha256(token)
    session.last_used_at = now()
    session.expires_at = now() + timedelta(days=request.app.state.settings.refresh_days)
    db.commit()
    return token_response(request, user, session, token)


@router.post("/logout", status_code=204)
def logout(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    auth.session.revoked_at = now()
    db.commit()


@router.get("/me")
def me(auth: Auth = Depends(current_auth)):
    return {**user_dict(auth.user), "mfa": auth.mfa}


@router.post("/password", status_code=204)
def change_password(body: PasswordBody, request: Request, auth: Auth = Depends(current_auth),
                    db: Session = Depends(get_db)):
    limit(request, "login", request.app.state.settings.rate_login)
    if not security.verify_password(auth.user.password_hash, body.current_password):
        raise error(401, "invalid_credentials", "Současné heslo nesouhlasí.")
    check_password(body.new_password)
    auth.user.password_hash = security.hash_password(body.new_password)
    # Ostatní zařízení se musí přihlásit znovu.
    db.execute(update(UserSession).where(
        UserSession.user_id == auth.user.id, UserSession.id != auth.session.id,
        UserSession.revoked_at.is_(None)).values(revoked_at=now()))
    db.commit()


@router.post("/totp/setup")
def totp_setup(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    if auth.user.totp_enabled:
        raise error(409, "totp_already_enabled", "Dvoufázové ověření je už zapnuté.")
    auth.user.totp_secret = security.new_totp_secret()
    db.commit()
    return {"secret": auth.user.totp_secret, "uri": security.totp_uri(auth.user.totp_secret, auth.user.email)}


@router.post("/totp/enable")
def totp_enable(body: CodeBody, request: Request, auth: Auth = Depends(current_auth),
                db: Session = Depends(get_db)):
    limit(request, "login", request.app.state.settings.rate_login)
    user = auth.user
    if user.totp_enabled:
        raise error(409, "totp_already_enabled", "Dvoufázové ověření je už zapnuté.")
    if not user.totp_secret:
        raise error(409, "totp_not_started", "Nejprve spusťte nastavení dvoufázového ověření.")
    step = security.totp_verify(user.totp_secret, body.code, user.totp_last_step)
    if step is None:
        raise error(401, "invalid_totp", "Kód nesouhlasí. Zkontrolujte čas v telefonu a zkuste další kód.")
    user.totp_enabled = True
    user.totp_last_step = step
    auth.session.mfa = True
    # Relace vzniklé před zapnutím 2FA končí.
    db.execute(update(UserSession).where(
        UserSession.user_id == user.id, UserSession.id != auth.session.id,
        UserSession.revoked_at.is_(None)).values(revoked_at=now()))
    db.commit()
    settings = request.app.state.settings
    return {"access_token": security.make_access_token(settings.jwt_secret, user.id, auth.session.id, True,
                                                       settings.access_minutes),
            "expires_in": settings.access_minutes * 60, "mfa": True, "user": user_dict(user)}
