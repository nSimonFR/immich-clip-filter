# The sidecar image. Immich is run with Docker Compose by almost everybody, so
# this is the primary distribution; the Nix flake is second.
#
# Deliberately plain: no build stage, no wheels cache, no compiler. The package is
# pure Python with exactly one dependency (psycopg2-binary, which ships wheels for
# amd64 and arm64), so a multi-stage build would trade real complexity for a few
# megabytes.
FROM python:3.12-slim

# tini so the container answers SIGTERM promptly — the sidecar is a blocking
# `serve_forever()` and would otherwise sit through the whole 10s stop timeout on
# every `docker compose restart`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Owned by a non-root user: the sidecar reads one Postgres table and writes one
# SQLite file, and there is nothing here that needs root.
RUN useradd --system --create-home --uid 10001 clip \
 && mkdir -p /var/lib/immich-clip \
 && chown -R clip:clip /var/lib/immich-clip
USER clip

# Container defaults differ from the library defaults, and both are right:
# listening on loopback is correct for a same-host install and useless in a
# container, where the plugin reaches the sidecar over the compose network.
ENV IMMICH_CLIP_LISTEN=0.0.0.0:8351 \
    IMMICH_CLIP_STATE_DIR=/var/lib/immich-clip \
    PYTHONUNBUFFERED=1

VOLUME ["/var/lib/immich-clip"]
EXPOSE 8351

# GET / is the liveness endpoint; it touches neither Postgres nor Immich, so a
# healthy container here means "the process is up", not "everything is fine".
# `immich-clip-doctor` is the one that answers the second question.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8351/', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["immich-clip-sidecar"]
