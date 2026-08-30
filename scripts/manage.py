"""Admin CLI - user accounts and one-off maintenance.

    python scripts/manage.py create-user alice --admin
    python scripts/manage.py list-users
    python scripts/manage.py reset-password alice
    python scripts/manage.py disable-user alice
    python scripts/manage.py enable-user alice
    python scripts/manage.py secret-key

Passwords are prompted for, never passed as arguments, so they stay out of
your shell history and the process list.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import User  # noqa: E402

MIN_PASSWORD_LENGTH = 12


def _prompt_password(username: str) -> str:
    while True:
        first = getpass.getpass(f"Password for {username}: ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"  Too short - use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        second = getpass.getpass("Repeat password: ")
        if first != second:
            print("  Passwords did not match. Try again.")
            continue
        return first


def create_user(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()
    with session_scope() as session:
        if session.scalars(select(User).where(User.username == username)).first():
            print(f"User {username!r} already exists.")
            return 1
        password = _prompt_password(username)
        session.add(
            User(
                username=username,
                display_name=args.name or args.username,
                email=args.email,
                password_hash=hash_password(password),
                source="local",
                is_admin=args.admin,
                is_active=True,
            )
        )
    role = "admin" if args.admin else "user"
    print(f"Created {role} {username!r}.")
    return 0


def list_users(_: argparse.Namespace) -> int:
    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.username)).all()
        if not users:
            print("No users yet. Create one with: manage.py create-user <name> --admin")
            return 0
        print(f"{'USERNAME':<20} {'SOURCE':<8} {'ADMIN':<6} {'ACTIVE':<7} LAST LOGIN")
        for u in users:
            last = u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "never"
            print(
                f"{u.username:<20} {u.source:<8} "
                f"{'yes' if u.is_admin else 'no':<6} "
                f"{'yes' if u.is_active else 'no':<7} {last}"
            )
    return 0


def reset_password(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()
    with session_scope() as session:
        user = session.scalars(select(User).where(User.username == username)).first()
        if not user:
            print(f"No such user: {username!r}")
            return 1
        if user.source != "local":
            print(f"{username!r} is an {user.source} account - change it there.")
            return 1
        user.password_hash = hash_password(_prompt_password(username))
    print(f"Password updated for {username!r}.")
    return 0


def _set_active(username: str, active: bool) -> int:
    username = username.strip().lower()
    with session_scope() as session:
        user = session.scalars(select(User).where(User.username == username)).first()
        if not user:
            print(f"No such user: {username!r}")
            return 1
        user.is_active = active
    print(f"{username!r} is now {'enabled' if active else 'disabled'}.")
    return 0


def secret_key(_: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(48))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="create a local account")
    p.add_argument("username")
    p.add_argument("--name", help="display name")
    p.add_argument("--email")
    p.add_argument("--admin", action="store_true")
    p.set_defaults(func=create_user)

    p = sub.add_parser("list-users")
    p.set_defaults(func=list_users)

    p = sub.add_parser("reset-password")
    p.add_argument("username")
    p.set_defaults(func=reset_password)

    p = sub.add_parser("disable-user")
    p.add_argument("username")
    p.set_defaults(func=lambda a: _set_active(a.username, False))

    p = sub.add_parser("enable-user")
    p.add_argument("username")
    p.set_defaults(func=lambda a: _set_active(a.username, True))

    p = sub.add_parser("secret-key", help="generate a SECRET_KEY value")
    p.set_defaults(func=secret_key)

    args = parser.parse_args()
    init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
