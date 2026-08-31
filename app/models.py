"""Database schema."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


SWAP_STATUSES = [
    "not_started",
    "planned",
    "ordered",
    "installed",
    "verified",
    "excluded",
]


class Phone(Base):
    __tablename__ = "phones"
    __table_args__ = (UniqueConstraint("device_name", name="uq_phones_device_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- from AXL --------------------------------------------------------
    device_name: Mapped[str] = mapped_column(String(64), index=True)
    pkid: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(255))
    model_raw: Mapped[str | None] = mapped_column(String(128))
    model_key: Mapped[str | None] = mapped_column(String(16), index=True)
    protocol: Mapped[str | None] = mapped_column(String(32))
    device_pool: Mapped[str | None] = mapped_column(String(128), index=True)
    site: Mapped[str | None] = mapped_column(String(128), index=True)
    configured_load: Mapped[str | None] = mapped_column(String(128))
    directory_number: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- from RisPort ----------------------------------------------------
    registration_status: Mapped[str | None] = mapped_column(String(32), index=True)
    status_reason: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64), index=True)
    active_load: Mapped[str | None] = mapped_column(String(128))
    cm_node: Mapped[str | None] = mapped_column(String(128))

    # --- from the phone's own web server ---------------------------------
    serial_number: Mapped[str | None] = mapped_column(String(64), index=True)
    hardware_revision: Mapped[str | None] = mapped_column(String(64))
    switch_name: Mapped[str | None] = mapped_column(String(128), index=True)
    switch_port: Mapped[str | None] = mapped_column(String(64))
    switch_ip: Mapped[str | None] = mapped_column(String(64))
    vlan_id: Mapped[str | None] = mapped_column(String(16))
    web_reachable: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- derived from the model catalog ----------------------------------
    family: Mapped[str | None] = mapped_column(String(32))
    generation: Mapped[str | None] = mapped_column(String(32), index=True)
    lifecycle: Mapped[str | None] = mapped_column(String(32), index=True)
    poe_class: Mapped[int | None] = mapped_column(Integer)
    poe_watts: Mapped[float | None] = mapped_column(Float)
    replacement_key: Mapped[str | None] = mapped_column(String(16), index=True)
    replacement_name: Mapped[str | None] = mapped_column(String(128))
    replacement_poe_watts: Mapped[float | None] = mapped_column(Float)

    # --- project tracking, edited by humans in the UI ---------------------
    swap_status: Mapped[str] = mapped_column(
        String(32), default="not_started", index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)

    # --- bookkeeping ------------------------------------------------------
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PhoneSnapshot(Base):
    """One row per phone per collection run — the raw material for change
    history ("what appeared, moved, dropped, re-registered") and fleet trends.
    Deliberately narrow: only the fields we diff or trend, keyed by the CUCM
    device name so it survives a phone row being deleted and re-created."""

    __tablename__ = "phone_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(Integer, index=True)
    device_name: Mapped[str] = mapped_column(String(64), index=True)

    registration_status: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    switch_name: Mapped[str | None] = mapped_column(String(128))
    switch_port: Mapped[str | None] = mapped_column(String(64))
    active_load: Mapped[str | None] = mapped_column(String(128))
    has_serial: Mapped[bool] = mapped_column(Boolean, default=False)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255))

    # Null for LDAP-sourced accounts - we never store an AD password.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="local")

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username} source={self.source} admin={self.is_admin}>"


class CallStat(Base):
    """Per-device call activity, aggregated from CUCM CDR/CMR files.

    Voxa keeps aggregates, not raw call records: the planning question is "which
    phones does nobody use, so we needn't replace them?" plus rough call quality.
    Keyed by CUCM device name so it lines up with the phone inventory.
    """

    __tablename__ = "call_stats"
    __table_args__ = (
        UniqueConstraint("device_name", name="uq_call_stats_device_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_name: Mapped[str] = mapped_column(String(64), index=True)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    inbound_calls: Mapped[int] = mapped_column(Integer, default=0)
    outbound_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mos_sum: Mapped[float] = mapped_column(Float, default=0.0)
    mos_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    @property
    def avg_mos(self) -> float | None:
        return round(self.mos_sum / self.mos_count, 2) if self.mos_count else None

    @property
    def total_minutes(self) -> int:
        return round(self.total_seconds / 60)


class Location(Base):
    """A dispatchable location for E911. Voxa-owned data (never from CUCM)."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LocationRule(Base):
    """Maps discovered data (switch name prefix or IP subnet) to a Location.

    match_type is "switch" (pattern is a switch-name prefix, e.g. `hq-acc`) or
    "subnet" (pattern is a CIDR like `10.20.1.0/24`, or an IP prefix). The most
    specific rule — longest pattern — wins.
    """

    __tablename__ = "location_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(Integer, index=True)
    match_type: Mapped[str] = mapped_column(String(16))
    pattern: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    stage: Mapped[str | None] = mapped_column(String(64))
    axl_count: Mapped[int] = mapped_column(Integer, default=0)
    ris_count: Mapped[int] = mapped_column(Integer, default=0)
    web_count: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 1)
