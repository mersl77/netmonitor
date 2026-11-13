"""Profily jednoho účtu. Zápis kontroluje revizi (optimistické zamykání): konflikt = HTTP 409."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import Auth, current_auth, error, get_db
from .models import Profile, aware, now

router = APIRouter(prefix="/profiles", tags=["profiles"])


class ProfileCreate(BaseModel):
    name: str = Field(max_length=100)
    config: dict


class ProfileUpdate(BaseModel):
    revision: int = Field(ge=1)
    config: dict | None = None
    name: str | None = Field(default=None, max_length=100)


def clean_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise error(422, "invalid_name", "Název profilu nesmí být prázdný.")
    return name


def check_config(request: Request, config: dict) -> dict:
    if len(json.dumps(config, ensure_ascii=False).encode("utf-8")) > request.app.state.settings.max_config_bytes:
        raise error(413, "config_too_large", "Konfigurace profilu je příliš velká.")
    if not isinstance(config.get("ping"), dict):
        raise error(422, "invalid_config", "Konfigurace neobsahuje nastavení ping.")
    return config


def owned(db: Session, auth: Auth, profile_id: uuid.UUID) -> Profile:
    obj = db.scalar(select(Profile).where(Profile.id == profile_id, Profile.user_id == auth.user.id))
    if obj is None:
        raise error(404, "not_found", "Profil nenalezen.")       # cizí profil se neliší od neexistujícího
    return obj


def summary(obj: Profile) -> dict:
    updated = aware(obj.updated_at)
    return {"id": str(obj.id), "name": obj.name, "revision": obj.revision,
            "updated_at": updated.isoformat() if updated else None}


def full(obj: Profile) -> dict:
    return {**summary(obj), "config": obj.config}


@router.get("")
def list_profiles(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    rows = db.scalars(select(Profile).where(Profile.user_id == auth.user.id).order_by(Profile.name_key)).all()
    return [summary(p) for p in rows]


@router.post("", status_code=201)
def create_profile(body: ProfileCreate, request: Request, auth: Auth = Depends(current_auth),
                   db: Session = Depends(get_db)):
    name = clean_name(body.name)
    check_config(request, body.config)
    count = db.scalar(select(func.count()).select_from(Profile).where(Profile.user_id == auth.user.id))
    if count >= request.app.state.settings.max_profiles:
        raise error(409, "profile_limit", "Dosažen maximální počet profilů na účet.")
    obj = Profile(user_id=auth.user.id, name=name, name_key=name.casefold(), config=body.config)
    db.add(obj)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise error(409, "name_exists", "Profil s tímto názvem už na serveru existuje.") from exc
    return full(obj)


@router.get("/{profile_id}")
def get_profile(profile_id: uuid.UUID, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    return full(owned(db, auth, profile_id))


@router.put("/{profile_id}")
def update_profile(profile_id: uuid.UUID, body: ProfileUpdate, request: Request,
                   auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    values: dict = {"revision": body.revision + 1, "updated_at": now()}
    if body.config is not None:
        values["config"] = check_config(request, body.config)
    if body.name is not None:
        values["name"] = clean_name(body.name)
        values["name_key"] = values["name"].casefold()
    # Atomická výměna: souběžné uložení stejné revize uspěje jen jednou.
    try:
        result = db.execute(update(Profile).where(
            Profile.id == profile_id, Profile.user_id == auth.user.id, Profile.revision == body.revision
        ).values(**values))
    except IntegrityError as exc:
        db.rollback()
        raise error(409, "name_exists", "Profil s tímto názvem už na serveru existuje.") from exc
    if result.rowcount != 1:
        db.rollback()
        owned(db, auth, profile_id)
        raise error(409, "revision_conflict", "Profil mezitím změnilo jiné zařízení.")
    db.commit()
    return full(owned(db, auth, profile_id))


@router.delete("/{profile_id}", status_code=204)
def delete_profile(profile_id: uuid.UUID, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    db.delete(owned(db, auth, profile_id))
    db.commit()
