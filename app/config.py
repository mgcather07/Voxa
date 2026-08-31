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

    # App behaviour
    app_name: str = "Voxa"
    site_from_device_pool: str = r"^(?:DP_)?(?P<site>[A-Za-z0-9]+)"
    mock_mode: bool = False

    # CDR/CMR ingest — the directory CUCM's Billing Application Server pushes
    # call detail / call quality CSVs to (the OS/SFTP lands them here). Read by
    # scripts/ingest_cdr.py; the app never opens an SFTP connection itself.
    cdr_dir: str = "/var/lib/voxa/cdr"

    # SNMP polling of access switches for real PoE draw / budget (read-only).
    # Needs pysnmp (requirements-snmp.txt). Switches are the CDP neighbours Voxa
    # already discovered (Phone.switch_ip); this is the SNMPv2c community.
    snmp_enabled: bool = False
    snmp_community: str = "public"
    snmp_timeout: int = 2

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
