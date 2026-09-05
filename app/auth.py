"""Authentication.

Deliberately structured around a pluggable backend so that swapping local
accounts for Active Directory later is a config change plus one new class,
not a rewrite of every route. Routes only ever call `require_user`; they never
know or care where the credentials were checked.

Password hashing uses stdlib scrypt. No bcrypt/passlib/argon2 dependency to
install, audit, or keep current on a locked-down server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from typing import Protocol

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_session
from .models import User

log = logging.getLogger(__name__)

# scrypt parameters. N must be a power of two; these are the interactive-login
# figures from the scrypt paper, comfortably fast on a VM and slow on a GPU.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(key).decode(),
        ]
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class AuthBackend(Protocol):
    """Anything that can turn a username + password into a User row."""

    def authenticate(
        self, session: Session, username: str, password: str
    ) -> User | None: ...


class LocalAuthBackend:
    """Accounts stored in our own database. Managed by scripts/manage.py."""

    name = "local"

    def authenticate(
        self, session: Session, username: str, password: str
    ) -> User | None:
        user = session.scalars(
            select(User).where(User.username == username.strip().lower())
        ).first()
        # Hash even when the user does not exist, so a missing account and a
        # wrong password take the same amount of time to reject.
        stored = user.password_hash if user else None
        ok = verify_password(password, stored)
        if not ok or user is None or not user.is_active:
            return None
        return user


class LdapAuthBackend:
    """Active Directory / LDAP bind authentication.

    The flow (configured in Settings → LDAP / Active Directory):

      1. Bind to ldap_url as ldap_bind_dn / ldap_bind_password (service acct).
      2. Search ldap_user_base_dn with ldap_user_filter for the username.
      3. Re-bind as that user's DN with the supplied password. Success there
         is the authentication — the AD password is never seen again or stored.
      4. Optionally require membership of ldap_required_group.
      5. Upsert a local User row with source="ldap" and no password_hash, so
         the rest of the app (audit trail, admin flags) works identically.

    Deployed behind ChainedAuthBackend, which checks local accounts first —
    the break-glass path that keeps admins able to sign in when every domain
    controller is unreachable. ldap3 ships in the image (requirements-ldap.txt).
    """

    name = "ldap"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(
        self, session: Session, username: str, password: str
    ) -> User | None:
        # ldap3 is an optional dependency — only LDAP deployments need it.
        # See requirements-ldap.txt.
        try:
            import ldap3
            from ldap3.core.exceptions import LDAPBindError, LDAPException
            from ldap3.utils.conv import escape_filter_chars
        except ImportError as exc:  # pragma: no cover - env dependent
            raise NotImplementedError(
                "LDAP auth needs ldap3: pip install -r requirements-ldap.txt"
            ) from exc

        from . import settings_store

        s = settings_store.load(session)
        if not (s.ldap_url and s.ldap_bind_dn and s.ldap_user_base_dn):
            raise NotImplementedError(
                "LDAP is not configured (set LDAP_URL, LDAP_BIND_DN, "
                "LDAP_USER_BASE_DN in Settings)."
            )
        # Never allow an empty password: many directories treat an empty bind
        # password as an anonymous bind, which would succeed and be a bypass.
        if not password:
            return None

        server = ldap3.Server(
            s.ldap_url,
            use_ssl=s.ldap_url.lower().startswith("ldaps"),
            get_info=ldap3.NONE,
        )
        try:
            # 1. Bind as the service account and find the user's DN + groups.
            svc = ldap3.Connection(
                server, s.ldap_bind_dn, s.ldap_bind_password, auto_bind=True
            )
            flt = s.ldap_user_filter.format(
                username=escape_filter_chars(username.strip())
            )
            svc.search(
                s.ldap_user_base_dn,
                flt,
                attributes=["displayName", "mail", "memberOf"],
            )
            if not svc.entries:
                svc.unbind()
                return None
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            display = str(entry.displayName) if "displayName" in entry else username
            email = str(entry.mail) if "mail" in entry else None
            groups = [str(g) for g in entry.memberOf] if "memberOf" in entry else []
            svc.unbind()

            # 2. Re-bind as the user with the supplied password — this is the
            #    actual authentication. We never store the AD password.
            try:
                user_conn = ldap3.Connection(
                    server, user_dn, password, auto_bind=True
                )
                user_conn.unbind()
            except LDAPBindError:
                log.warning("LDAP bind failed for %r", username)
                return None

            # 3. Group gate.
            if s.ldap_required_group and s.ldap_required_group not in groups:
                log.warning("LDAP user %r not in required group", username)
                return None
        except LDAPException as exc:  # pragma: no cover - env dependent
            # Directory unreachable / service bind rejected. Details go to the
            # log; the login page gets a clean message. Local break-glass
            # accounts were already tried by ChainedAuthBackend.
            log.error("LDAP error authenticating %r: %s", username, exc)
            raise NotImplementedError(
                "Active Directory sign-in is unavailable (directory unreachable "
                "or service account rejected — see the server log). Local "
                "break-glass accounts still work."
            ) from exc

        # 4. Upsert a local shadow account (source=ldap, no password stored) so
        #    the rest of the app — audit trail, admin flags — works identically.
        uname = username.strip().lower()
        user = session.scalars(select(User).where(User.username == uname)).first()
        # An account an admin deactivated stays locked out — a successful
        # directory bind does not override it.
        if user is not None and not user.is_active:
            return None
        if user is None:
            # is_active is set explicitly: the column default only applies at
            # flush, and checking the un-flushed None here rejected every AD
            # user's FIRST login (caught by the backend test).
            user = User(username=uname, source="ldap", is_admin=False,
                        is_active=True)
            session.add(user)
        user.display_name = display
        user.email = email
        user.source = "ldap"
        session.commit()
        return user


class ChainedAuthBackend:
    """LDAP with a local break-glass path.

    Local accounts (rows with a password hash in our own database) are checked
    FIRST, so an admin can always sign in even when every domain controller is
    down or LDAP is misconfigured. Anything that isn't a local match falls
    through to the directory. LDAP shadow accounts carry no password hash, so
    they can never match the local step — there is no way to bypass AD with a
    guessed local password for a directory user.
    """

    name = "ldap+local"

    def __init__(self, local: LocalAuthBackend, ldap: LdapAuthBackend) -> None:
        self.local = local
        self.ldap = ldap

    def authenticate(
        self, session: Session, username: str, password: str
    ) -> User | None:
        user = self.local.authenticate(session, username, password)
        if user is not None:
            return user
        return self.ldap.authenticate(session, username, password)


def get_backend(settings: Settings | None = None) -> AuthBackend:
    from . import settings_store

    settings = settings or get_settings()
    if settings_store.load().auth_backend == "ldap":
        return ChainedAuthBackend(LocalAuthBackend(), LdapAuthBackend(settings))
    return LocalAuthBackend()


def test_ldap(session: Session, test_username: str, test_password: str = "") -> list[dict]:
    """Step-by-step LDAP configuration check for the Settings page.

    Uses the SAVED LDAP settings. Read-only against the directory: a service
    bind, a user search, and (only if a test password was supplied) a bind as
    that user. Returns [{ok, check, detail}, ...] — the same shape the cluster
    connection test renders.
    """
    checks: list[dict] = []

    def add(ok: bool, check: str, detail: str) -> None:
        checks.append({"ok": ok, "check": check, "detail": detail})

    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        add(False, "ldap3 installed", "pip install -r requirements-ldap.txt")
        return checks
    add(True, "ldap3 installed", ldap3.version.__version__)

    from . import settings_store

    s = settings_store.load(session)
    missing = [k for k, v in {
        "LDAP URL": s.ldap_url, "Bind DN": s.ldap_bind_dn,
        "User base DN": s.ldap_user_base_dn,
    }.items() if not (v or "").strip()]
    if missing:
        add(False, "Configuration", f"missing: {', '.join(missing)} — set and Save above")
        return checks
    add(True, "Configuration", f"{s.ldap_url} · base {s.ldap_user_base_dn}")

    server = ldap3.Server(
        s.ldap_url, use_ssl=s.ldap_url.lower().startswith("ldaps"),
        get_info=ldap3.NONE, connect_timeout=5,
    )
    try:
        svc = ldap3.Connection(
            server, s.ldap_bind_dn, s.ldap_bind_password,
            auto_bind=True, receive_timeout=8,
        )
    except LDAPException as exc:
        add(False, "Service-account bind", f"{type(exc).__name__}: {exc}")
        return checks
    add(True, "Service-account bind", f"bound as {s.ldap_bind_dn}")

    if not (test_username or "").strip():
        add(True, "User lookup", "enter a test username to check the search filter")
        svc.unbind()
        return checks

    flt = s.ldap_user_filter.format(
        username=escape_filter_chars(test_username.strip())
    )
    try:
        svc.search(s.ldap_user_base_dn, flt,
                   attributes=["displayName", "mail", "memberOf"])
    except LDAPException as exc:
        add(False, "User lookup", f"search failed: {exc}")
        svc.unbind()
        return checks
    if not svc.entries:
        add(False, "User lookup", f"no match for filter {flt} under {s.ldap_user_base_dn}")
        svc.unbind()
        return checks
    entry = svc.entries[0]
    user_dn = entry.entry_dn
    groups = [str(g) for g in entry.memberOf] if "memberOf" in entry else []
    add(True, "User lookup", f"{user_dn} · {len(groups)} group(s)")

    if s.ldap_required_group:
        in_group = s.ldap_required_group in groups
        add(in_group, "Required group",
            s.ldap_required_group if in_group
            else f"user is NOT in {s.ldap_required_group}")
    svc.unbind()

    if test_password:
        try:
            uc = ldap3.Connection(server, user_dn, test_password,
                                  auto_bind=True, receive_timeout=8)
            uc.unbind()
            add(True, "User bind (password check)", "credentials accepted")
        except LDAPException as exc:
            add(False, "User bind (password check)", f"{type(exc).__name__}: {exc}")
    else:
        add(True, "User bind", "no test password supplied — lookup only")
    return checks


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
SESSION_USER_KEY = "uid"


def login_user(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


class RedirectToLogin(HTTPException):
    """Raised instead of a 401 so browsers land on the login form."""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__(status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        self.next_url = next_url


def require_user(
    request: Request, user: User | None = Depends(current_user)
) -> User:
    """Route dependency. Every page except /login and /health uses this."""
    if get_settings().auth_disabled:
        return User(
            id=0, username="anonymous", display_name="Auth disabled", is_admin=True
        )
    if user is None:
        raise RedirectToLogin(next_url=str(request.url.path))
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an admin account.",
        )
    return user


def redirect_to_login(next_url: str = "/") -> RedirectResponse:
    target = "/login"
    if next_url and next_url != "/":
        target = f"/login?next={next_url}"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
