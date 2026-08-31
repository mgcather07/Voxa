"""Opt-in outbound webhooks.

Voxa is batch (it ingests CDR files after the fact), so these fire on the events
Voxa already computes — after a collection or a CDR ingest — not per call in
realtime. Everything here is DORMANT unless BOTH the global WEBHOOKS_ENABLED
switch is on AND a Webhook row is enabled, so nothing ever leaves the app by
default. Payloads are signed with HMAC-SHA256 so the receiver can verify them.

This is the one place Voxa sends data outward; it still never writes to CUCM.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import Webhook, utcnow

log = logging.getLogger("voxa")

EVENTS = ("phones.changed", "call.quality_alert", "sync.completed")


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deliver(hook: Webhook, event: str, payload: dict) -> str:
    body = json.dumps(
        {"event": event, "sent_at": utcnow().isoformat(), "data": payload},
        default=str,
    ).encode()
    headers = {"Content-Type": "application/json", "X-Voxa-Event": event}
    if hook.secret:
        headers["X-Voxa-Signature"] = "sha256=" + sign(hook.secret, body)
    try:
        resp = httpx.post(hook.url, content=body, headers=headers, timeout=5)
        status = str(resp.status_code)
    except Exception as exc:  # noqa: BLE001 - never let a webhook break a sync
        status = f"error: {type(exc).__name__}"
        log.warning("Webhook %s -> %s failed: %s", event, hook.url, exc)
    hook.last_status = status
    hook.last_fired_at = utcnow()
    return status


def fire(event: str, payload: dict) -> None:
    """Deliver an event to every enabled webhook subscribed to it. No-op unless
    the global switch is on."""
    if not get_settings().webhooks_enabled:
        return
    with session_scope() as session:
        for hook in session.scalars(
            select(Webhook).where(Webhook.enabled.is_(True))
        ).all():
            subs = hook.event_list()
            if not subs or event in subs:
                log.info("Firing webhook %s -> %s", event, hook.url)
                _deliver(hook, event, payload)


def send_test(session: Session, hook: Webhook) -> str:
    """Admin-initiated test. Still gated by the global switch so a disabled
    instance sends nothing."""
    if not get_settings().webhooks_enabled:
        return "webhooks disabled (set WEBHOOKS_ENABLED=true)"
    status = _deliver(hook, "test", {"message": "Voxa webhook test"})
    session.commit()
    return status
