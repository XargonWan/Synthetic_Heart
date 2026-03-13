ARG TARGETPLATFORM
# 1. Grab uv binary from its official image
FROM ghcr.io/astral-sh/uv:latest AS uv_source

# 2. Start your actual Base Image (Selkies/Ubuntu)
FROM --platform=$TARGETPLATFORM ghcr.io/linuxserver/baseimage-selkies:ubuntunoble

ARG TARGETARCH
ARG GITVERSION_TAG
ARG BUILD_DATE
ARG VERSION

LABEL build_version="Synthetic Heart version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="xargonwan"

# --- [Standard Env Setup] ---
ENV TITLE="Synthetic Heart"
ENV PIXELFLUX_USE_XSHM=0 \
    PIXELFLUX_DISABLE_XSHM=1 \
    PIXELFLUX_NO_XSHM=1 \
    QT_X11_NO_MITSHM=1 \
    DISABLE_XSHM=1 \
    BROWSER=/usr/local/bin/chromium-browser
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
      espeak-ng libespeak-ng1 && \
    update-ca-certificates --fresh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# --- [Browser & Desktop Setup (Unchanged)] ---
# Install gemini-cli (uv can handle this too, but pip is fine for single tools)
RUN pip3 install --no-cache-dir gemini-cli

# Install Chromium
RUN ARCH="${TARGETARCH}" && \
    if [ -z "$ARCH" ]; then echo "Warning: TARGETARCH not set, defaulting to amd64" && ARCH=amd64; fi && \
    apt-get update && \
    apt-get purge -y google-chrome google-chrome-stable || true && \
    apt-get install -y --no-install-recommends debian-archive-keyring && \
    echo "deb [arch=$ARCH signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian bookworm main" > /etc/apt/sources.list.d/debian-chromium.list && \
    echo "deb [arch=$ARCH signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://security.debian.org/debian-security bookworm-security main" >> /etc/apt/sources.list.d/debian-chromium.list && \
    apt-get update && \
    CHROMIUM_VERSION=$(apt-cache policy chromium | awk '/Candidate:/ {print $2}') && \
    apt-get install -y --no-install-recommends chromium=$CHROMIUM_VERSION chromium-driver=$CHROMIUM_VERSION && \
    apt-mark hold chromium chromium-driver && \
    rm -f /etc/apt/sources.list.d/debian-chromium.list && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    chromium --version

# Chromium Profile & Desktop shortcuts
RUN mkdir -p '/config/.config/chromium-synth' && \
    chown -R abc:abc /config && \
    chmod -R 775 /config && \
    mkdir -p /usr/local/share/applications && \
    echo '[Desktop Entry]' > /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Version=1.0' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Name=Chromium SyntH' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Exec=/usr/bin/chromium --no-sandbox --user-data-dir=/config/.config/chromium-synth %U' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Terminal=false' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Type=Application' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Categories=Network;WebBrowser;' >> /usr/local/share/applications/chromium-synth.desktop && \
    chmod 644 /usr/local/share/applications/chromium-synth.desktop && \
    mkdir -p /config/.local/share/applications && \
    cp /usr/local/share/applications/chromium-synth.desktop /config/.local/share/applications/ && \
    chown -R abc:abc /config/.local

# Install XFCE4
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      xfce4 xfce4-goodies xfce4-terminal thunar mousepad ristretto \
      adwaita-icon-theme util-linux dbus-x11 at-spi2-core \
      pulseaudio pulseaudio-utils pavucontrol \
      espeak-ng && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

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
COPY webtop/s6-services/synth /etc/s6-overlay/s6-rc.d/synth
RUN chmod +x /etc/s6-overlay/s6-rc.d/synth/run && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo synth > /etc/s6-overlay/s6-rc.d/user/contents.d/synth && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/synth

RUN echo xfce4-session > /config/desktop-session

# S6 Websockify
COPY webtop/s6-services/websockify /etc/s6-overlay/s6-rc.d/websockify
RUN chmod +x /etc/s6-overlay/s6-rc.d/websockify/run && \
    echo 'longrun' > /etc/s6-overlay/s6-rc.d/websockify/type && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo websockify > /etc/s6-overlay/s6-rc.d/user/contents.d/websockify && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/websockify

# Final cleanup
RUN mv /usr/bin/thunar /usr/bin/thunar-real && \
  rm -f /etc/xdg/autostart/xfce4-power-manager.desktop /etc/xdg/autostart/xscreensaver.desktop && \
  rm -rf /tmp/*

COPY webtop/root /

# Permissions
RUN chown -R abc:abc /app && \
    mkdir -p /app/logs && chown -R abc:abc /app/logs