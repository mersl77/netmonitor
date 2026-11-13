"""Správa z příkazové řádky uvnitř kontejneru (Portainer → kontejner → Console, nebo docker exec).

    python -m app.cli create-admin     první/další účet správce (heslo se zadává skrytě)
    python -m app.cli create-key "poznámka" [--uses N] [--days N]
    python -m app.cli list-users
    python -m app.cli reset-password <e-mail>
    python -m app.cli reset-totp <e-mail>      při ztrátě telefonu správce
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from . import security, settings as settings_mod
from .auth import EMAIL, MIN_PASSWORD
from .main import make_engine
from .models import Base, LicenseKey, User, UserSession, now


def ask_password() -> str:
    first = getpass.getpass(f"Heslo (alespoň {MIN_PASSWORD} znaků): ")
    if len(first) < MIN_PASSWORD:
        raise SystemExit("Heslo je příliš krátké.")
    if getpass.getpass("Heslo znovu: ") != first:
        raise SystemExit("Hesla se neshodují.")
    return first


def find(db, email: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().casefold()))
    if user is None:
        raise SystemExit("Účet nenalezen.")
    return user


def revoke_sessions(db, user: User) -> None:
    db.execute(update(UserSession).where(
        UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).values(revoked_at=now()))


def main(argv=None, db_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-admin")
    key = sub.add_parser("create-key")
    key.add_argument("note")
    key.add_argument("--uses", type=int, default=1)
    key.add_argument("--days", type=int)
    sub.add_parser("list-users")
    for name in ("reset-password", "reset-totp"):
        sub.add_parser(name).add_argument("email")
    args = parser.parse_args(argv)

    if db_factory is None:
        engine = make_engine(settings_mod.load().database_url)
        Base.metadata.create_all(engine)
        db_factory = sessionmaker(engine, expire_on_commit=False)
    with db_factory() as db:
        if args.cmd == "create-admin":
            email = input("E-mail správce: ").strip().casefold()
            if not EMAIL.fullmatch(email):
                raise SystemExit("Neplatný e-mail.")
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                if input("Účet existuje. Povýšit na správce? [a/N] ").strip().lower() != "a":
                    return 1
                user.is_admin = True
            else:
                db.add(User(email=email, password_hash=security.hash_password(ask_password()), is_admin=True))
            db.commit()
            print("Správce je připraven. V aplikaci se přihlaste a na kartě Správa zapněte dvoufázové ověření.")
        elif args.cmd == "create-key":
            plain = security.new_license_key()
            db.add(LicenseKey(key_hash=security.sha256(plain), prefix=plain[:8], note=args.note.strip()[:200],
                              max_uses=max(1, args.uses),
                              expires_at=now() + timedelta(days=args.days) if args.days else None))
            db.commit()
            print(plain)
        elif args.cmd == "list-users":
            for u in db.scalars(select(User).order_by(User.created_at)).all():
                flags = ("správce " if u.is_admin else "") + ("2FA " if u.totp_enabled else "") + \
                        ("" if u.is_active else "ZABLOKOVÁN")
                print(f"{u.email}\t{flags.strip()}")
        elif args.cmd == "reset-password":
            user = find(db, args.email)
            user.password_hash = security.hash_password(ask_password())
            user.failed_logins, user.locked_until = 0, None
            revoke_sessions(db, user)
            db.commit()
            print("Heslo změněno, všechna zařízení odhlášena.")
        elif args.cmd == "reset-totp":
            user = find(db, args.email)
            user.totp_enabled, user.totp_secret, user.totp_last_step = False, None, 0
            revoke_sessions(db, user)
            db.commit()
            print("Dvoufázové ověření vypnuto, všechna zařízení odhlášena. Zapněte je znovu v aplikaci.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
