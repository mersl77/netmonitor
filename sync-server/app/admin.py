"""Správa licenčních klíčů a účtů. Jen pro správce přihlášeného s dvoufázovým ověřením."""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import security
from .deps import Auth, current_admin, error, get_db
from .models import LicenseKey, Profile, User, UserSession, aware, now

router = APIRouter(prefix="/admin", tags=["admin"])


class KeyCreate(BaseModel):
    note: str = Field(min_length=1, max_length=200)          # pro koho klíč je
    max_uses: int = Field(default=1, ge=1, le=1000)
    valid_days: int | None = Field(default=None, ge=1, le=3650)


def iso(value) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


def key_state(key: LicenseKey) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and aware(key.expires_at) <= now():
        return "expired"
    return "used" if key.uses >= key.max_uses else "active"


def key_dict(key: LicenseKey, emails: list[str]) -> dict:
    return {"id": str(key.id), "prefix": key.prefix, "note": key.note, "max_uses": key.max_uses,
            "uses": key.uses, "state": key_state(key), "created_at": iso(key.created_at),
            "expires_at": iso(key.expires_at), "revoked_at": iso(key.revoked_at), "users": emails}


@router.get("/keys")
def list_keys(_: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    owners: dict[uuid.UUID, list[str]] = {}
    for email, key_id in db.execute(select(User.email, User.license_key_id).where(User.license_key_id.is_not(None))):
        owners.setdefault(key_id, []).append(email)
    keys = db.scalars(select(LicenseKey).order_by(LicenseKey.created_at.desc())).all()
    return [key_dict(k, sorted(owners.get(k.id, []))) for k in keys]


@router.post("/keys", status_code=201)
def create_key(body: KeyCreate, _: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    note = body.note.strip()
    if not note:
        raise error(422, "invalid_note", "Vyplňte poznámku, pro koho klíč je.")
    plain = security.new_license_key()
    key = LicenseKey(key_hash=security.sha256(plain), prefix=plain[:8], note=note, max_uses=body.max_uses,
                     expires_at=now() + timedelta(days=body.valid_days) if body.valid_days else None)
    db.add(key)
    db.commit()
    # Celý klíč se vrací jen teď; v databázi je pouze jeho otisk.
    return {**key_dict(key, []), "key": plain}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: uuid.UUID, _: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    key = db.get(LicenseKey, key_id)
    if key is None:
        raise error(404, "not_found", "Klíč nenalezen.")
    if key.revoked_at is None:
        key.revoked_at = now()
        db.commit()
    emails = db.scalars(select(User.email).where(User.license_key_id == key.id)).all()
    return key_dict(key, sorted(emails))


@router.get("/users")
def list_users(_: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Profile.user_id, func.count()).group_by(Profile.user_id)).all())
    notes = {k.id: k.note for k in db.scalars(select(LicenseKey)).all()}
    return [{"id": str(u.id), "email": u.email, "is_admin": u.is_admin, "is_active": u.is_active,
             "totp_enabled": u.totp_enabled, "created_at": iso(u.created_at),
             "last_login_at": iso(u.last_login_at), "profiles": counts.get(u.id, 0),
             "key_note": notes.get(u.license_key_id, "")}
            for u in db.scalars(select(User).order_by(User.created_at)).all()]


def set_active(user_id: uuid.UUID, active: bool, auth: Auth, db: Session) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise error(404, "not_found", "Účet nenalezen.")
    if user.id == auth.user.id:
        raise error(409, "self_change", "Vlastní účet takto změnit nelze.")
    user.is_active = active
    if not active:
        db.execute(update(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).values(revoked_at=now()))
    db.commit()
    return {"id": str(user.id), "email": user.email, "is_active": user.is_active}


@router.post("/users/{user_id}/disable")
def disable_user(user_id: uuid.UUID, auth: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    return set_active(user_id, False, auth, db)


@router.post("/users/{user_id}/enable")
def enable_user(user_id: uuid.UUID, auth: Auth = Depends(current_admin), db: Session = Depends(get_db)):
    return set_active(user_id, True, auth, db)
