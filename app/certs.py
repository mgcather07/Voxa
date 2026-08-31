"""Read the TLS certificate each CUCM node serves, via a plain handshake.

Fully read-only: open a TLS socket to a port and inspect the leaf certificate
the node presents (subject, issuer, validity, SAN). No CUCM API, no writes —
same principle as the rest of Voxa. Certs not served on a socket (e.g.
ITLRecovery) would need the version-specific PAWS API and are out of scope.
stdlib only — no new dependency.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import settings_store
from .models import Certificate, ClusterNode

log = logging.getLogger(__name__)

# CUCM TLS services worth checking. Tomcat (8443) breaks web / AXL / phone
# services on expiry; CallManager (5061) breaks SIP-TLS trunks and phones; CAPF
# (2443) breaks LSC issuance.
PORTS = [
    (8443, "Tomcat (Web / AXL)"),
    (5061, "CallManager (SIP-TLS)"),
    (2443, "CAPF"),
]


def _cn(rdn_seq) -> str | None:
    """commonName from an ssl subject/issuer RDN sequence, else organizationName."""
    fallback = None
    for rdn in rdn_seq or ():
        for key, value in rdn:
            if key == "commonName":
                return value
            if key == "organizationName" and fallback is None:
                fallback = value
    return fallback


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(
            ssl.cert_time_to_seconds(value), tz=timezone.utc
        )
    except (ValueError, OverflowError, OSError):
        return None


def fetch_cert(host: str, port: int, timeout: float = 4.0) -> dict:
    """Parsed fields of the leaf cert served at host:port, or {'error': ...}."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # inspecting, not validating trust
    try:
        with socket.create_connection((host, port), timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
    except Exception as exc:  # noqa: BLE001 - DNS / route / TLS all surface here
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    if not der:
        return {"error": "no certificate presented"}

    pem = ssl.DER_cert_to_PEM_cert(der)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    try:
        tmp.write(pem)
        tmp.close()
        info = ssl._ssl._test_decode_cert(tmp.name)  # stdlib cert decoder
    except Exception as exc:  # noqa: BLE001
        return {"error": f"decode failed: {exc}"[:200]}
    finally:
        os.unlink(tmp.name)

    subject = info.get("subject", ())
    issuer = info.get("issuer", ())
    san = ", ".join(f"{t}:{v}" for t, v in info.get("subjectAltName", ()))
    return {
        "subject_cn": _cn(subject),
        "issuer_cn": _cn(issuer),
        "self_signed": subject == issuer,
        "valid_from": _dt(info.get("notBefore")),
        "valid_to": _dt(info.get("notAfter")),
        "san": san or None,
        "serial": info.get("serialNumber"),
    }


def _targets(session: Session) -> list[tuple[str, str]]:
    """(host, friendly-label) to probe: cluster connection hosts + every node."""
    seen: dict[str, str] = {}
    for conn in settings_store.clusters(session):
        seen.setdefault(conn.host, f"{conn.name} (connected)")
    for node in session.scalars(select(ClusterNode)).all():
        seen.setdefault(node.name, node.description or node.name)
    return list(seen.items())


def collect(session: Session, timeout: float = 4.0) -> dict:
    """Probe every (target, port), replacing the stored Certificate rows.
    Read-only against CUCM. Returns a small summary."""
    jobs = [
        (host, label, port, svc)
        for host, label in _targets(session)
        for port, svc in PORTS
    ]

    def probe(job):
        host, label, port, svc = job
        return host, label, port, svc, fetch_cert(host, port, timeout)

    now = datetime.now(timezone.utc)
    rows: list[Certificate] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for host, label, port, svc, r in pool.map(probe, jobs):
            rows.append(Certificate(
                node=label, host=host, port=port, service=svc,
                subject_cn=r.get("subject_cn"), issuer_cn=r.get("issuer_cn"),
                self_signed=bool(r.get("self_signed")),
                valid_from=r.get("valid_from"), valid_to=r.get("valid_to"),
                san=r.get("san"), serial=r.get("serial"),
                error=r.get("error"), checked_at=now,
            ))

    session.execute(delete(Certificate))
    for row in rows:
        session.add(row)
    return {"targets": len({j[0] for j in jobs}), "checked": len(rows),
            "ok": sum(1 for r in rows if not r.error)}
