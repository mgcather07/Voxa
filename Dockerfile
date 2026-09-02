FROM python:3.12-slim AS base

# Stamped at build time (`make image` passes the git describe version). Baked in
# as a label and env var so a running container can report exactly what it is.
ARG VOXA_VERSION=dev

LABEL org.opencontainers.image.title="Voxa" \
      org.opencontainers.image.description="CUCM call telemetry and phone refresh planner (read-only)" \
      org.opencontainers.image.version="${VOXA_VERSION}" \
      org.opencontainers.image.source="https://github.com/mgcather07/Voxa"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VOXA_VERSION=${VOXA_VERSION}

WORKDIR /srv/app

# Dependencies first so code edits do not invalidate the layer. The SFTP extra
# (paramiko) is baked in because CDR-over-SFTP is a first-class feature here;
# it stays in its own requirements file so other deployments can omit it.
COPY requirements.txt requirements-sftp.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-sftp.txt

COPY app ./app
COPY config ./config
COPY scripts ./scripts

# Run unprivileged. The app writes nothing to disk - state lives in Postgres.
RUN useradd --system --uid 10001 --create-home appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Two workers is right for a few thousand phones. The sync job holds a
# database lock via the SyncRun row, so extra workers cannot double-run it.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
