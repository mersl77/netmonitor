from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from . import security
from .models import User, UserSession, aware, now


def error(status: int, code: str, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def get_db(request: Request):
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(request: Request, bucket: str, rule: tuple[int, int]) -> None:
    wait = request.app.state.limiter.check(f"{bucket}:{client_ip(request)}", rule[0], rule[1])
    if wait:
        raise error(429, "rate_limited", f"Příliš mnoho požadavků. Zkuste to za {wait} s.",
                    {"Retry-After": str(wait)})


@dataclass
class Auth:
    user: User
    session: UserSession
    mfa: bool


def current_auth(request: Request, authorization: str | None = Header(default=None),
                 db: Session = Depends(get_db)) -> Auth:
    if not authorization or not authorization.startswith("Bearer "):
        raise error(401, "unauthorized", "Chybí přístupový token.")
    try:
        claims = security.read_access_token(request.app.state.settings.jwt_secret, authorization[7:])
        user_id, session_id = uuid.UUID(str(claims["sub"])), uuid.UUID(str(claims["sid"]))
    except (ValueError, KeyError, jwt.PyJWTError) as exc:
        raise error(401, "unauthorized", "Neplatný nebo expirovaný token.") from exc
    session = db.get(UserSession, session_id)
    user = db.get(User, user_id)
    if (user is None or not user.is_active or session is None or session.user_id != user.id
            or session.revoked_at is not None or aware(session.expires_at) <= now()):
        raise error(401, "unauthorized", "Relace byla ukončena. Přihlaste se znovu.")
    return Auth(user=user, session=session, mfa=bool(session.mfa))


def current_admin(auth: Auth = Depends(current_auth)) -> Auth:
    if not auth.user.is_admin:
        raise error(403, "forbidden", "Vyžaduje účet správce.")
    if not auth.user.totp_enabled:
        raise error(403, "totp_setup_required", "Správce musí nejprve zapnout dvoufázové ověření.")
    if not auth.mfa:
        raise error(403, "totp_required", "Přihlaste se znovu s kódem z autentikátoru.")
    return auth
