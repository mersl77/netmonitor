"""Profilové API pro existující NetMonitor Sync FastAPI server.

Určeno k integraci do /opt/netmonitor-sync/api/app na serveru aplikacni.
Nepoužívat jako standalone: očekává stávající app.database, app.models
a jwt_secret z Docker secretu.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import Profile, User

router = APIRouter(prefix="/profiles", tags=["profiles"])
JWT_SECRET = Path("/run/secrets/jwt_secret")
MAX_CONFIG_BYTES = 512 * 1024


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    config: dict

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Prázdný název profilu")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict) -> dict:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("Konfigurace překračuje limit 512 KiB")
        if not isinstance(value.get("ping"), dict):
            raise ValueError("Konfigurace neobsahuje ping")
        return value


class ProfileUpdate(BaseModel):
    config: dict
    revision: int = Field(ge=1)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict) -> dict:
        return ProfileCreate.validate_config(value)


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Chybí přístupový token")
    token = authorization[7:]
    try:
        secret = JWT_SECRET.read_text(encoding="utf-8").strip()
        claims = jwt.decode(
            token, secret, algorithms=["HS256"],
            issuer="netmonitor-sync", audience="netmonitor-desktop",
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        uid = UUID(str(claims["sub"]))
    except (OSError, ValueError, jwt.PyJWTError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Neplatný nebo expirovaný token") from exc
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Uživatel neexistuje")
    return user


def profile_owned(db: Session, user: User, profile_id: UUID) -> Profile:
    obj = db.scalar(select(Profile).where(
        Profile.id == profile_id, Profile.user_id == user.id
    ))
    if obj is None:
        raise HTTPException(status_code=404, detail="Profil nenalezen")
    return obj


def serialize(obj: Profile) -> dict:
    return {
        "id": str(obj.id), "name": obj.name, "config": obj.config,
        "revision": obj.revision,
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
    }


@router.get("")
def list_profiles(user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = db.scalars(select(Profile).where(Profile.user_id == user.id).order_by(Profile.name)).all()
    return [{"id": str(p.id), "name": p.name, "revision": p.revision,
             "updated_at": p.updated_at.isoformat() if p.updated_at else None}
            for p in result]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_profile(
    body: ProfileCreate, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    exists = db.scalar(select(Profile.id).where(
        Profile.user_id == user.id, Profile.name == body.name
    ))
    if exists is not None:
        raise HTTPException(status_code=409, detail="Profil s tímto názvem existuje")
    obj = Profile(
        id=uuid4(), user_id=user.id, name=body.name,
        config=body.config, revision=1, updated_at=datetime.now(timezone.utc),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return serialize(obj)


@router.get("/{profile_id}")
def get_profile(
    profile_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return serialize(profile_owned(db, user, profile_id))


@router.put("/{profile_id}")
def update_profile(
    profile_id: UUID, body: ProfileUpdate,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    # Atomická CAS aktualizace; souběžné uložení stejné revize nesmí uspět dvakrát.
    from sqlalchemy import update
    result = db.execute(
        update(Profile).where(
            Profile.id == profile_id,
            Profile.user_id == user.id,
            Profile.revision == body.revision,
        ).values(
            config=body.config,
            revision=body.revision + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount != 1:
        db.rollback()
        profile_owned(db, user, profile_id)  # 404, pokud jde o cizí/neexistující profil
        raise HTTPException(status_code=409, detail="Zastaralá revize profilu")
    db.commit()
    return serialize(profile_owned(db, user, profile_id))


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    obj = profile_owned(db, user, profile_id)
    db.delete(obj)
    db.commit()
    return None
