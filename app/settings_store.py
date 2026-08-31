"""Runtime configuration: DB-backed operational settings on top of env defaults.

Two tiers of config:

* **Bootstrap** (env only, via `config.Settings`): what's needed to start the app
  and reach its own database — DATABASE_URL, SECRET_KEY, AUTH_DISABLED, sessions.
  You can't store the database connection *in* the database.
* **Operational** (here): everything site-specific — CUCM clusters, phone-scrape
  tuning, SNMP, LDAP, CDR path, webhooks. Edited in the Settings UI; a DB value
  overrides the env default. Secrets are stored in the DB and masked in the UI
  (same plaintext-at-rest posture as .env, restricted by database access).

`load()` returns a plain namespace (values copied out of the session) so callers
can use it anywhere. `clusters()` returns configured CUCM connections, falling
back to a single env-derived cluster so nothing breaks before anything is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import Cluster, Setting

# key, type, secret, group, label
SCHEMA = [
    ("app_name", "str", False, "Application", "App name"),
    ("phone_web_enabled", "bool", False, "Discovery", "Scrape phones for serial/switch"),
    ("phone_web_timeout", "int", False, "Discovery", "Phone web timeout (s)"),
    ("phone_web_concurrency", "int", False, "Discovery", "Phone scrape concurrency"),
    ("site_from_device_pool", "str", False, "Discovery", "Site-from-device-pool regex"),
    ("cdr_dir", "str", False, "CDR", "CDR/CMR drop directory"),
    ("snmp_enabled", "bool", False, "SNMP", "Enable SNMP PoE polling"),
    ("snmp_community", "str", True, "SNMP", "SNMP community"),
    ("snmp_timeout", "int", False, "SNMP", "SNMP timeout (s)"),
    ("auth_backend", "str", False, "LDAP / Active Directory", "Auth backend (local | ldap)"),
    ("ldap_url", "str", False, "LDAP / Active Directory", "LDAP URL"),
    ("ldap_bind_dn", "str", False, "LDAP / Active Directory", "Bind DN"),
    ("ldap_bind_password", "str", True, "LDAP / Active Directory", "Bind password"),
    ("ldap_user_base_dn", "str", False, "LDAP / Active Directory", "User base DN"),
    ("ldap_user_filter", "str", False, "LDAP / Active Directory", "User filter"),
    ("ldap_required_group", "str", False, "LDAP / Active Directory", "Required group"),
    ("webhooks_enabled", "bool", False, "Webhooks", "Enable outbound webhooks (master switch)"),
]
_BY_KEY = {row[0]: row for row in SCHEMA}
SECRET_KEYS = {row[0] for row in SCHEMA if row[2]}
SECRET_MASK = "••••••••"


def _coerce(value: str, kind: str):
    if kind == "bool":
        return str(value).strip().lower() in {"1", "true", "on", "yes"}
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value


def _rows(session: Session) -> dict[str, str]:
    return {s.key: s.value for s in session.scalars(select(Setting)).all()}


def load(session: Session | None = None) -> SimpleNamespace:
    """Resolved operational config (DB overrides env). Safe to use anywhere."""
    if session is None:
        with session_scope() as s:
            return load(s)
    env = get_settings()
    stored = _rows(session)
    out = {}
    for key, kind, _secret, _group, _label in SCHEMA:
        if key in stored:
            out[key] = _coerce(stored[key], kind)
        else:
            out[key] = getattr(env, key)
    return SimpleNamespace(**out)


def form_values(session: Session) -> dict:
    """Current values for the settings form — secrets replaced with a mask."""
    cfg = load(session)
    values = {}
    for key, kind, secret, _group, _label in SCHEMA:
        v = getattr(cfg, key)
        if secret:
            values[key] = SECRET_MASK if v else ""
        else:
            values[key] = v
    return values


def save(session: Session, data: dict) -> None:
    """Upsert settings from a submitted form. Unchecked bools become false; a
    secret left as the mask (or blank) is preserved, not overwritten."""
    existing = {s.key: s for s in session.scalars(select(Setting)).all()}
    for key, kind, secret, _group, _label in SCHEMA:
        if kind == "bool":
            new = "true" if data.get(key) else "false"
        else:
            raw = (data.get(key) or "").strip()
            if secret and (raw == "" or raw == SECRET_MASK):
                continue  # keep the stored secret
            new = raw
        row = existing.get(key)
        if row is None:
            session.add(Setting(key=key, value=new, is_secret=secret))
        else:
            row.value = new
            row.is_secret = secret
    session.commit()


@dataclass
class ClusterConn:
    name: str
    host: str
    user: str
    password: str
    axl_version: str
    verify_tls: bool
    phone_web_enabled: bool


def clusters(session: Session | None = None) -> list[ClusterConn]:
    """Enabled CUCM clusters from the DB, or a single env-derived default so the
    app works before any cluster has been configured in the UI."""
    if session is None:
        with session_scope() as s:
            return clusters(s)
    rows = session.scalars(
        select(Cluster).where(Cluster.enabled.is_(True)).order_by(Cluster.id)
    ).all()
    if rows:
        return [
            ClusterConn(
                name=r.name, host=r.axl_host, user=r.cucm_user,
                password=r.cucm_password, axl_version=r.axl_version,
                verify_tls=r.verify_tls, phone_web_enabled=r.phone_web_enabled,
            )
            for r in rows
        ]
    env = get_settings()
    return [ClusterConn(
        name=env.cluster_name, host=env.cucm_host, user=env.cucm_user,
        password=env.cucm_password, axl_version=env.cucm_axl_version,
        verify_tls=env.cucm_verify_tls, phone_web_enabled=env.phone_web_enabled,
    )]


def primary_host(session: Session | None = None) -> str:
    conns = clusters(session)
    return conns[0].host if conns else get_settings().cucm_host
