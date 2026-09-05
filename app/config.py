"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # CUCM
    cucm_host: str = "cucm-pub.example.local"
    cucm_user: str = ""
    cucm_password: str = ""
    cucm_axl_version: str = "12.5"
    cucm_verify_tls: bool = False
    # Label for the cluster this instance collects from. Multi-cluster is
    # supported by running the collector once per cluster, each with its own
    # CUCM_* env and a distinct CLUSTER_NAME; every phone is tagged with it.
    cluster_name: str = "default"

    # Phone web scraping
    phone_web_enabled: bool = True
    phone_web_timeout: int = 4
    phone_web_concurrency: int = 25

    # Database
    database_url: str = (
        "postgresql+psycopg://voxa:voxa@localhost:5433/voxa"
    )

    # Deployment environment. "production" (the safe default) enforces the
    # boot-time guards in app.main: no AUTH_DISABLED, no default SECRET_KEY.
    # The dev stack sets VOXA_ENV=development to relax them.
    voxa_env: str = "production"

    # Build version, stamped into the image at build time (Dockerfile ARG ->
    # VOXA_VERSION env). Shown on the About page.
    voxa_version: str = "dev"

    # App behaviour
    app_name: str = "Voxa"
    # IANA timezone for DISPLAYING timestamps (storage stays UTC). e.g.
    # "America/Chicago". Empty/UTC shows UTC.
    display_timezone: str = "UTC"

    # Shown on the About page. Set per customer/reseller in Settings.
    licensed_to: str = ""
    support_contact: str = ""
    site_from_device_pool: str = r"^(?:DP_)?(?P<site>[A-Za-z0-9]+)"
    mock_mode: bool = False

    # CDR/CMR ingest — the directory CUCM's Billing Application Server pushes
    # call detail / call quality CSVs to (the OS/SFTP lands them here). Read by
    # scripts/ingest_cdr.py; the app never opens an SFTP connection itself.
    cdr_dir: str = "/var/lib/voxa/cdr"
    # Keep ingested CDR/CMR files in cdr_dir/processed/ this many days, then
    # prune (0 = keep forever). Bounds local disk on a live feed.
    cdr_retention_days: int = 0

    # Optional SFTP pull of CDR/CMR files. When enabled, Voxa connects to the
    # SFTP server the Billing Application Server pushes to, downloads new files
    # into cdr_dir, then ingest folds them. Needs paramiko (requirements-sftp.txt).
    # Reads from *your* SFTP server, never from CUCM; the password is stored in
    # the DB and masked in the UI, same posture as the CUCM password.
    cdr_sftp_enabled: bool = False
    cdr_sftp_host: str = ""
    cdr_sftp_port: int = 22
    cdr_sftp_user: str = ""
    cdr_sftp_password: str = ""
    cdr_sftp_dir: str = ""
    cdr_sftp_delete: bool = False  # remove each file from the server after download
    # Auto-pull + ingest every N minutes when SFTP is enabled (0 = manual only,
    # click "Pull & ingest now"). Runs inside the app — no cron needed.
    cdr_pull_interval_min: int = 0

    # SNMP polling of access switches for real PoE draw / budget (read-only).
    # Needs pysnmp (requirements-snmp.txt). Switches are the CDP neighbours Voxa
    # already discovered (Phone.switch_ip); this is the SNMPv2c community.
    snmp_enabled: bool = False
    snmp_community: str = "public"
    snmp_timeout: int = 2

    # Outbound webhooks — OFF by default. Master switch; individual Webhook rows
    # also carry an `enabled` flag. When off, nothing ever leaves the app.
    webhooks_enabled: bool = False

    # Authentication
    # secret_key signs the session cookie. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str = "dev-only-insecure-key-change-me"
    auth_backend: str = "local"          # local | ldap (ldap not implemented)
    auth_disabled: bool = False          # local development escape hatch only
    session_max_age: int = 60 * 60 * 12  # 12 hours
    session_https_only: bool = False     # set true once nginx terminates TLS

    # LDAP / Active Directory - reserved, see docs/ROADMAP.md
    ldap_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_required_group: str = ""

    @property
    def secret_is_default(self) -> bool:
        return self.secret_key == "dev-only-insecure-key-change-me"

    @property
    def is_production(self) -> bool:
        return self.voxa_env.strip().lower() not in {"development", "dev", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
