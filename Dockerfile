# syntax=docker/dockerfile:1
#
# The image holds the Core and what the Core needs. Everything else is installed at run
# time, from the official extension repository (a built-in, non-removable repository of
# the extension runtime): the default extensions are downloaded and installed on the
# first start. Services that belong to one plugin (Zen, SearXNG) are separate containers
# in docker-compose.yml. (The S6 overlay, the in-container SearXNG and the baked-in extensions
# were removed in commit 805c5d53: `git log --diff-filter=D -- container/s6-services`.)

# 1. The uv binary, from its official image.
FROM ghcr.io/astral-sh/uv:latest AS uv_source

# 2. Builder: the Python environment. Nothing from here but /app/venv reaches the final
# image, so no uv cache, no build tools and no dev dependencies end up in it.
# python:3.12-slim has to be the same image as the final stage: the venv points at its
# interpreter.
FROM python:3.12-slim AS builder
COPY --from=uv_source /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_LINK_MODE=copy
# --frozen: the exact versions of uv.lock. --no-dev: no pytest, ruff, sphinx. The project
# itself is not installed: the code is on PYTHONPATH.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# 3. The final image.
FROM python:3.12-slim

ARG GITVERSION_TAG=0.0.0-dev
ARG BUILD_DATE
ARG VERSION

LABEL build_version="Synthetic Heart version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="xargonwan"

ENV TITLE="Synthetic Heart"
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# --- [System packages: only what the Core runs] ---
#   tini                 PID 1: reaps the processes extensions spawn
#   ca-certificates      TLS            openssl   the WebUI's self-signed certificate
#   git                  repositories of kind "git" (github releases need no git)
#   ffmpeg               audio and video conversion
#   postgresql-client    pg_dump for the database backups
#   libegl1 ... mesa     headless avatar renderer (software GL, no GPU, no display)
# No compiler, no editors, no MariaDB client: every Python dependency is a wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates openssl git \
      ffmpeg postgresql-client \
      libegl1 libegl-mesa0 libgl1 libgl1-mesa-dri \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash abc

# core/avatar_render draws the SyntH's face for video calls on the server with moderngl.
# moderngl's glcontext opens "libEGL.so" / "libGL.so" by their unversioned names, which
# Debian only ships in the -dev packages: link them to the runtime libraries (works on
# amd64 and arm64).
RUN for lib in libEGL libGL; do \
      target="$(ldconfig -p | awk -v n="$lib.so.1" '$1 == n { print $NF; exit }')" && \
      test -n "$target" && ln -sf "$target" "/usr/local/lib/$lib.so" ; \
    done && ldconfig

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/venv
COPY --from=builder /app/venv /app/venv

# Build-time check of the headless renderer: prints the GL implementation, or a loud warning
# (the build is not failed: calls then simply stay audio-only, see docs/call_service.rst).
RUN EGL_PLATFORM=surfaceless /app/venv/bin/python -c "import moderngl; c = moderngl.create_standalone_context(backend='egl'); print('avatar renderer GL:', c.info['GL_RENDERER'])" \
    || echo "WARNING: no headless GL context in this image: video calls will be audio-only"

# --- [App] ---
# The application code. Rei is not copied here: the default skin lives once, in
# /app/.skins-seed (the entrypoint seeds it into the skins volume).
COPY --exclude=skins/Rei . /app
COPY skins/Rei /app/.skins-seed/Rei
# The launcher runs /app/venv/bin/python directly (the repository's own synth.sh is for
# development and uses `uv run`; uv is not in this image).
RUN chmod +x /app/container/entrypoint.sh /app/automation_tools/container_synth.sh

ENV PYTHONPATH=/app
# The resolved version, available at runtime as os.getenv("SYNTH_VERSION") (read by
# core/webui.py). GITVERSION_TAG is supplied by CI (scripts/version.py); local builds fall
# back to the ARG default above.
ENV SYNTH_VERSION=$GITVERSION_TAG

ENTRYPOINT ["/usr/bin/tini", "--", "/app/container/entrypoint.sh"]
