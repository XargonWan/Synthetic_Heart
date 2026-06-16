# 1. Grab uv binary from its official image
FROM ghcr.io/astral-sh/uv:latest AS uv_source

# 2. Start your actual Base Image (Debian slim)
# PyTorch official wheels require a glibc-based Python environment.
FROM python:3.12-slim

ARG TARGETARCH
ARG GITVERSION_TAG
ARG BUILD_DATE
ARG VERSION

LABEL build_version="Synthetic Heart version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="xargonwan"

# --- [Standard Env Setup] ---
ENV TITLE="Synthetic Heart"
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# --- [System Dependencies] ---
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash curl wget unzip nano vim \
      ca-certificates \
      openssl \
      htop net-tools iputils-ping \
  ffmpeg mariadb-client libmariadb-dev postgresql-client \
      espeak \
      xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash abc

# --- [S6 Overlay] ---
ENV S6_OVERLAY_VERSION=v3.2.2.0
RUN case "${TARGETARCH:-amd64}" in \
      amd64) overlay_arch=x86_64 ;; \
      arm64|aarch64) overlay_arch=aarch64 ;; \
      *) overlay_arch=x86_64 ;; \
    esac && \
    wget -qO /tmp/s6-overlay-${overlay_arch}.tar.xz \
      "https://github.com/just-containers/s6-overlay/releases/download/${S6_OVERLAY_VERSION}/s6-overlay-${overlay_arch}.tar.xz" && \
    wget -qO /tmp/s6-overlay-noarch.tar.xz \
      "https://github.com/just-containers/s6-overlay/releases/download/${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" && \
    tar xJf /tmp/s6-overlay-noarch.tar.xz -C / && \
    tar xJf /tmp/s6-overlay-${overlay_arch}.tar.xz -C / && \
    rm -f /tmp/s6-overlay-*.tar.xz

# --- [Inject UV] ---
# Copy the uv binary into /usr/local/bin so it's available globally
COPY --from=uv_source /uv /usr/local/bin/uv

# --- [CLI Tools Setup] ---
# gemini-cli is installed in the project venv via uv sync (pyproject dependency)
# (Avoid system pip install in PEP-668 managed environment.)

# --- [Python & UV Setup] ---
WORKDIR /app

# 1. Copy dependency files FIRST (for caching)
COPY pyproject.toml uv.lock ./
# (no vendored packages needed any more)
# (Optional fallback if you don't have lockfiles yet: COPY requirements.txt . )  # kept for backwards compatibility, not needed in normal builds

# 2. Tell uv to create the venv at /app/venv (Matching your old structure)
ENV UV_PROJECT_ENVIRONMENT=/app/venv

# 3. Create venv and Install Dependencies
# This replaces the old "python3 -m venv && pip install" block
# --frozen: Uses the exact versions from uv.lock
RUN uv sync --frozen --no-cache


# --- [App Setup] ---
# Copy scripts
COPY automation_tools/container_synth.sh /app/synth.sh
RUN chmod +x /app/synth.sh

# Copy application code (includes vendor packages)
COPY . /app

# vendored packages are installed by `uv sync` earlier via path sources.
# Historically we pip-installed them here, but that invoked the system pip
# outside of the UV_PROJECT_ENVIRONMENT and caused modules to be missing at
# runtime.  Keeping them solely under uv sync ensures they live in /app/venv.

# Cleanup & Permissions
RUN rm -rf /app/s6-services /app/automation_tools
ENV PYTHONPATH=/app
ENV GITVERSION_TAG=$GITVERSION_TAG
RUN echo "$GITVERSION_TAG" > /app/version.txt

# S6 Services Setup
COPY container/s6-services/synth /etc/s6-overlay/s6-rc.d/synth
RUN chmod +x /etc/s6-overlay/s6-rc.d/synth/run && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo synth > /etc/s6-overlay/s6-rc.d/user/contents.d/synth && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/synth

# Final cleanup
RUN rm -rf /tmp/*

ENTRYPOINT ["/init"]

