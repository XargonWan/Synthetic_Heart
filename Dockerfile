ARG TARGETPLATFORM
FROM --platform=$TARGETPLATFORM ghcr.io/linuxserver/baseimage-selkies:ubuntunoble

ARG TARGETARCH
ARG GITVERSION_TAG
ARG BUILD_DATE
ARG VERSION

LABEL build_version="Synthetic Heart version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="xargonwan"

ENV TITLE="Synthetic Heart"
ENV PIXELFLUX_USE_XSHM=0 \
    PIXELFLUX_DISABLE_XSHM=1 \
    PIXELFLUX_NO_XSHM=1 \
    QT_X11_NO_MITSHM=1 \
    DISABLE_XSHM=1 \
    BROWSER=/usr/local/bin/chromium-browser
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Block snap completely
RUN echo 'Package: snapd' > /etc/apt/preferences.d/no-snap && \
    echo 'Pin: release a=*' >> /etc/apt/preferences.d/no-snap && \
    echo 'Pin-Priority: -10' >> /etc/apt/preferences.d/no-snap && \
    apt-get update && \
    apt-get purge -y snapd && \
    apt-get autoremove -y && \
    rm -rf /snap /var/snap /var/lib/snapd && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Basic packages and Python setup
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
    git curl wget unzip nano vim \
      lsb-release ca-certificates \
    openssl \
      htop net-tools iputils-ping \
      ffmpeg mariadb-client libmariadb3 libmariadb-dev && \
    # Force update of CA certificates bundle
    update-ca-certificates --fresh && \
    # Ensure Python can find certificates
    python3 -m pip install --upgrade certifi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install gemini-cli
RUN pip3 install --no-cache-dir gemini-cli

# Install Chromium browser and driver without snap
RUN ARCH="${TARGETARCH}" && \
    if [ -z "$ARCH" ]; then \
        echo "Warning: TARGETARCH not set, defaulting to amd64" && \
        ARCH=amd64; \
    fi && \
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

# Prepare chrome profile folder
RUN mkdir -p '/config/.config/chromium-synth' && \
    chown -R abc:abc /config && \
    chmod -R 775 /config

# Set Chromium as default browser with profile
RUN mkdir -p /usr/local/share/applications && \
    echo '[Desktop Entry]' > /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Version=1.0' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Name=Chromium SyntH' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Comment=Chromium browser for Synthetic Heart' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Exec=/usr/bin/chromium --no-sandbox --user-data-dir=/config/.config/chromium-synth %U' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Terminal=false' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Type=Application' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'Categories=Network;WebBrowser;' >> /usr/local/share/applications/chromium-synth.desktop && \
    echo 'MimeType=text/html;text/xml;application/xhtml+xml;application/xml;x-scheme-handler/http;x-scheme-handler/https;' >> /usr/local/share/applications/chromium-synth.desktop && \
    chmod 644 /usr/local/share/applications/chromium-synth.desktop && \
    mkdir -p /config/.local/share/applications && \
    cp /usr/local/share/applications/chromium-synth.desktop /config/.local/share/applications/ && \
    chown -R abc:abc /config/.local && \
    su - abc -c 'xdg-settings set default-web-browser chromium-synth.desktop'

# Install XFCE4 desktop environment
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      xfce4 \
      xfce4-goodies \
      xfce4-terminal \
      thunar \
      mousepad \
      ristretto \
      adwaita-icon-theme \
      # adw-gtk3 \ # cannot find package
      util-linux \
      dbus-x11 \
      at-spi2-core \
      pulseaudio \
      pulseaudio-utils \
      pavucontrol && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy project code and set up Python venv in an isolated path to avoid accidental
# overwrites when project files are copied into /app or when bind-mounting the repo
COPY requirements.txt /app/requirements.txt
WORKDIR /app
ENV VENV_DIR=/opt/venv

# Create Python venv outside /app so that copying project files into /app will not
# overwrite the venv. Expose a symlink at /app/venv for backward compatibility.
RUN python3 -m venv $VENV_DIR && \
    $VENV_DIR/bin/pip install --no-cache-dir --upgrade pip setuptools && \
    $VENV_DIR/bin/pip install --no-cache-dir -r requirements.txt && \
    ln -sfn $VENV_DIR /app/venv

# Copy essential scripts
COPY automation_tools/cleanup_chrome.sh /usr/local/bin/cleanup_chrome.sh
COPY automation_tools/container_synth.sh /app/synth.sh
RUN chmod +x /usr/local/bin/cleanup_chrome.sh /app/synth.sh

# Create a default self-signed cert to include in the image (used as a seed at runtime)
RUN mkdir -p /app/res/default_ssl && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout /app/res/default_ssl/synth_webui.key \
      -out /app/res/default_ssl/synth_webui.crt \
      -subj '/CN=localhost' -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' || true

# Copy project code last to leverage layer caching
COPY . /app

# Verify venv integrity after copying project files: ensure the container's venv
# was not overwritten by a host 'venv' directory. This check will fail the build
# when a host virtualenv is accidentally copied, catching the root cause early.
RUN $VENV_DIR/bin/python3 -c "import sys, importlib; importlib.import_module('uvicorn'); print('uvicorn present after copy:', importlib.import_module('uvicorn').__version__)"

# Seed Selkies TLS certs into /config/ssl (used by HTTPS on port 3001)
RUN mkdir -p /config/ssl && \
    cp -n /app/res/default_ssl/synth_webui.crt /config/ssl/cert.pem && \
    cp -n /app/res/default_ssl/synth_webui.key /config/ssl/cert.key && \
    chown -R abc:abc /config/ssl

# Note: res/ is included in the image. No runtime seeding or res_template is
# created (we avoid keeping a mirrored /app/res_template). Static assets live
# inside /app/res and will be present in the image.

RUN rm -rf /app/s6-services /app/automation_tools
ENV PYTHONPATH=/app

# Inject GitVersion tag
ENV GITVERSION_TAG=$GITVERSION_TAG
RUN echo "$GITVERSION_TAG" > /app/version.txt && \
    echo "Building with tag: $GITVERSION_TAG"

# Create S6 service for synth
COPY webtop/s6-services/synth /etc/s6-overlay/s6-rc.d/synth
RUN chmod +x /etc/s6-overlay/s6-rc.d/synth/run && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo synth > /etc/s6-overlay/s6-rc.d/user/contents.d/synth && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/synth

# Set XFCE as default session for Selkies
RUN echo xfce4-session > /config/desktop-session

# Copy S6 synth service
COPY webtop/s6-services/synth /etc/s6-overlay/s6-rc.d/synth
RUN chmod +x /etc/s6-overlay/s6-rc.d/synth/run && \
    echo 'longrun' > /etc/s6-overlay/s6-rc.d/synth/type && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo synth > /etc/s6-overlay/s6-rc.d/user/contents.d/synth && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/synth

# Copy S6 Websockify service for Selkies
COPY webtop/s6-services/websockify /etc/s6-overlay/s6-rc.d/websockify
RUN chmod +x /etc/s6-overlay/s6-rc.d/websockify/run && \
    echo 'longrun' > /etc/s6-overlay/s6-rc.d/websockify/type && \
    mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    echo websockify > /etc/s6-overlay/s6-rc.d/user/contents.d/websockify && \
    chown -R abc:abc /etc/s6-overlay/s6-rc.d/websockify

# Do Webtop cleanup and tweaks
RUN mv \
    /usr/bin/thunar \
    /usr/bin/thunar-real && \
  echo "**** cleanup ****" && \
  rm -f \
    /etc/xdg/autostart/xfce4-power-manager.desktop \
    /etc/xdg/autostart/xscreensaver.desktop \
    /usr/share/xfce4/panel/plugins/power-manager-plugin.desktop && \
  rm -rf \
    /tmp/*

# Copy the root folder (used by original webtop, without chromium: https://github.com/linuxserver/docker-webtop/blob/master/Dockerfile)
COPY webtop/root /

# Set permissions for abc user
# Note: abc user home is /config
RUN chown -R abc:abc /app
# Ensure logs folder exists in image and is owned by the runtime user
RUN mkdir -p /app/logs && chown -R abc:abc /app/logs
