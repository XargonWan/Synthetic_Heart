ARG TARGETPLATFORM
# 1. Grab uv binary from its official image
FROM ghcr.io/astral-sh/uv:latest AS uv_source

# 2. Start your actual Base Image (Selkies/Ubuntu)
FROM --platform=$TARGETPLATFORM ghcr.io/linuxserver/baseimage-ubuntu:noble

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

# --- [Inject UV] ---
# Copy the uv binary into /usr/local/bin so it's available globally
COPY --from=uv_source /uv /usr/local/bin/uv

# --- [System Dependencies] ---
# Block snap & Install packages
RUN echo 'Package: snapd' > /etc/apt/preferences.d/no-snap && \
    echo 'Pin: release a=*' >> /etc/apt/preferences.d/no-snap && \
    echo 'Pin-Priority: -10' >> /etc/apt/preferences.d/no-snap && \
    apt-get update && \
    apt-get purge -y snapd && \
    apt-get autoremove -y && \
    rm -rf /snap /var/snap /var/lib/snapd && \
    # Added python3-full for venv support if needed, though uv handles it
    apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
    git curl wget unzip nano vim \
      lsb-release ca-certificates \
    openssl \
      htop net-tools iputils-ping \
      ffmpeg mariadb-client libmariadb3 libmariadb-dev \
      espeak-ng libespeak-ng1 \
      libatomic1 && \
    update-ca-certificates --fresh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

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
COPY automation_tools/cleanup_chrome.sh /usr/local/bin/cleanup_chrome.sh
COPY automation_tools/container_synth.sh /app/synth.sh
RUN chmod +x /usr/local/bin/cleanup_chrome.sh /app/synth.sh

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

# Permissions
RUN chown -R abc:abc /app && \
    mkdir -p /app/logs && chown -R abc:abc /app/logs