from sqlalchemy import select

from app import cli, security
from app.models import LicenseKey, User

from .conftest import PASSWORD, add_user, login, register


def test_create_admin_key_and_resets(app, client, monkeypatch, capsys):
    factory = app.state.session_factory
    monkeypatch.setattr("builtins.input", lambda *_: "Spravce@Example.org")
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: PASSWORD)
    assert cli.main(["create-admin"], factory) == 0
    with factory() as db:
        user = db.scalar(select(User))
        assert user.email == "spravce@example.org" and user.is_admin and not user.totp_enabled
    capsys.readouterr()
    assert cli.main(["create-key", "Jana – účetní", "--days", "7"], factory) == 0
    key = capsys.readouterr().out.strip()
    with factory() as db:
        obj = db.scalar(select(LicenseKey))
        assert obj.note == "Jana – účetní" and obj.key_hash == security.sha256(key) and obj.expires_at
    assert register(client, key).status_code == 201

    secret = add_user(app, "druhy@example.org", admin=True, totp=True)
    session = login(client, "druhy@example.org", secret=secret)
    assert cli.main(["reset-totp", "druhy@example.org"], factory) == 0
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + session.json()["access_token"]}).status_code == 401
    assert login(client, "druhy@example.org").status_code == 200       # bez kódu, 2FA je vypnuté

    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: "jine-dlouhe-heslo")
    assert cli.main(["reset-password", "druhy@example.org"], factory) == 0
    assert login(client, "druhy@example.org").status_code == 401
    assert login(client, "druhy@example.org", password="jine-dlouhe-heslo").status_code == 200
