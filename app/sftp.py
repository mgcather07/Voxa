"""Pull CUCM CDR/CMR files from the SFTP server the billing app pushes to.

Optional feature. CUCM's Billing Application Server SFTP-pushes CDR/CMR files to
an SFTP server you run; when the drop is on a different host than Voxa, this
fetches new files into ``cdr_dir`` so ingest can fold them — the same job cron +
rsync would do, exposed as a Settings form with a Test button instead.

Reads from *your* SFTP server, never from CUCM — the read-only-CUCM guarantee is
untouched. Needs paramiko (requirements-sftp.txt); the base image ships without
it, same as the LDAP/SNMP extras.

Idempotent: a file already present locally (in ``cdr_dir`` or its ``processed/``
archive) is skipped, so a re-pull never re-downloads and ingest never
double-counts — no matter whether files are deleted from the server after.
"""

from __future__ import annotations

import logging
import socket
import stat as statmod
from pathlib import Path

log = logging.getLogger(__name__)

# Only pull call-data files; never a partial transfer.
_EXCLUDE_SUFFIXES = (".tmp", ".part", ".filepart", ".writing")
_WANT_SUFFIXES = {".csv", ".txt", ""}

_TIMEOUT = 8.0


def _connect(cfg):
    """Open an SSH+SFTP session from the resolved settings. Raises
    NotImplementedError if paramiko isn't installed."""
    try:
        import paramiko  # type: ignore
    except ImportError as exc:  # pragma: no cover - env dependent
        raise NotImplementedError(
            "SFTP pull needs paramiko: pip install -r requirements-sftp.txt"
        ) from exc

    client = paramiko.SSHClient()
    # We're pulling from the operator's own server; accept the host key rather
    # than requiring a preloaded known_hosts on the VM.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.cdr_sftp_host,
        port=int(cfg.cdr_sftp_port or 22),
        username=cfg.cdr_sftp_user or None,
        password=cfg.cdr_sftp_password or None,
        timeout=_TIMEOUT,
        banner_timeout=_TIMEOUT,
        auth_timeout=_TIMEOUT,
        allow_agent=False,
        look_for_keys=False,
    )
    return client, client.open_sftp()


def _friendly(exc: Exception) -> str:
    """Turn a connection/transfer exception into an operator-readable reason."""
    try:
        import paramiko  # type: ignore
        if isinstance(exc, paramiko.AuthenticationException):
            return "Authentication failed — check the username and password."
        if isinstance(exc, paramiko.SSHException):
            return f"SSH error: {exc}"
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, socket.gaierror):
        return "Host not found — the SFTP hostname didn't resolve (DNS)."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "Connection timed out — host unreachable or wrong port."
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused — nothing is listening on that host/port."
    msg = str(exc)
    if isinstance(exc, (FileNotFoundError, IOError)) or "No such file" in msg:
        return "Remote directory not found — check the path."
    return f"{type(exc).__name__}: {msg}"


def _is_wanted(name: str) -> bool:
    low = name.lower()
    if low.endswith(_EXCLUDE_SUFFIXES):
        return False
    return Path(low).suffix in _WANT_SUFFIXES


def test_connection(cfg) -> dict:
    """Connect and list the remote directory. Returns {ok, message}."""
    if not (cfg.cdr_sftp_host or "").strip():
        return {"ok": False, "message": "No SFTP host set — fill in the fields, Save, then Test."}
    client = None
    try:
        client, sftp = _connect(cfg)
        remote_dir = cfg.cdr_sftp_dir or "."
        names = sftp.listdir(remote_dir)
        files = [n for n in names if _is_wanted(n)]
        host = cfg.cdr_sftp_host
        port = int(cfg.cdr_sftp_port or 22)
        return {
            "ok": True,
            "message": f"Connected to {host}:{port} — {len(files)} CDR/CMR "
                       f"file(s) waiting in {remote_dir}.",
        }
    except NotImplementedError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - report every failure to the UI
        return {"ok": False, "message": _friendly(exc)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def pull(cfg, dest: str | Path) -> dict:
    """Download new CDR/CMR files into ``dest``. Skips any name already present
    locally (in ``dest`` or ``dest/processed/``). Optionally deletes each file
    from the server after a successful download. Returns {ok, downloaded,
    skipped, message}."""
    if not (cfg.cdr_sftp_host or "").strip():
        return {"ok": False, "message": "No SFTP host set."}
    dest = Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": f"Can't write to the drop directory: {exc}"}

    seen = {p.name for p in dest.glob("*") if p.is_file()}
    seen |= {p.name for p in (dest / "processed").glob("*") if p.is_file()}

    client = None
    downloaded = skipped = 0
    try:
        client, sftp = _connect(cfg)
        remote_dir = cfg.cdr_sftp_dir or "."
        for name in sftp.listdir(remote_dir):
            if not _is_wanted(name):
                continue
            remote = f"{remote_dir.rstrip('/')}/{name}"
            try:
                if statmod.S_ISDIR(sftp.stat(remote).st_mode):
                    continue
            except OSError:
                continue
            if name in seen:
                skipped += 1
                continue
            tmp = dest / (name + ".part")
            sftp.get(remote, str(tmp))
            tmp.rename(dest / name)
            downloaded += 1
            if cfg.cdr_sftp_delete:
                try:
                    sftp.remove(remote)
                except OSError as exc:
                    log.warning("SFTP: downloaded %s but couldn't remove it from "
                                "the server: %s", name, exc)
        return {
            "ok": True, "downloaded": downloaded, "skipped": skipped,
            "message": f"Downloaded {downloaded} new file(s); "
                       f"skipped {skipped} already pulled.",
        }
    except NotImplementedError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": _friendly(exc)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
