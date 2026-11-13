"""Jednorázové vytvoření prvního uživatele v existující DB NetMonitor Sync.

Spouštět pouze v API kontejneru / v prostředí s přístupem k PostgreSQL
a nainstalovanými závislostmi. Heslo se nikdy nepředává jako CLI argument.
"""
from __future__ import annotations

import getpass
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import User

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def validate_email(raw: str) -> str:
    email = raw.strip().casefold()
    if len(email) > 254 or not EMAIL.fullmatch(email):
        raise ValueError("Neplatná e-mailová adresa")
    return email


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Heslo musí mít alespoň 12 znaků")
    if len(password) > 1024:
        raise ValueError("Heslo překračuje bezpečnostní limit")


def create_first_user(db: Session, email: str, password: str) -> User:
    """Z bezpečnostních důvodů nepovoluje vytvořit další účet."""
    email = validate_email(email)
    validate_password(password)
    if db.scalar(select(func.count()).select_from(User)):
        raise ValueError("Účet již existuje. Příkaz vytvoří pouze úplně prvního uživatele.")
    if db.scalar(select(User.id).where(User.email == email)):
        raise ValueError("E-mail již existuje")
    user = User(id=uuid4(), email=email,
                password_hash=PasswordHasher().hash(password),
                created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main() -> int:
    print("NetMonitor Sync – založení prvního uživatele")
    try:
        email = validate_email(input("E-mail: "))
        pwd = getpass.getpass("Heslo (alespoň 12 znaků): ")
        again = getpass.getpass("Heslo znovu: ")
        if pwd != again:
            print("Hesla se neshodují", file=sys.stderr)
            return 1
        validate_password(pwd)
        with Session(engine) as db:
            user = create_first_user(db, email, pwd)
        print(f"Účet vytvořen: {user.email} (ID: {user.id})")
        print("Nyní lze použít POST /auth/login nebo přihlašovací formulář NetMonitor.")
        return 0
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(f"Účet nebyl vytvořen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
