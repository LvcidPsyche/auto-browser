FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fluxbox \
    fonts-noto-core \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxkbcommon0 \
    nodejs \
    novnc \
    npm \
    procps \
    websockify \
    x11vnc \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

# Chromium, Xvfb, x11vnc and noVNC run as this unprivileged user. Chromium runs
# without its own sandbox (there is no user namespace to build one in under
# Docker's default seccomp profile), so root inside the container was the whole
# of what a renderer exploit started with. entrypoint.sh starts as root only to
# fix ownership of the mounted data directories, then drops to this user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin browser

WORKDIR /opt/browser-node

COPY package.json package-lock.json ./
# PLAYWRIGHT_BROWSERS_PATH (above) puts Chromium in /ms-playwright rather than
# root's home, which the unprivileged user cannot read.
RUN npm ci --omit=dev --no-audit --no-fund \
  && npx playwright install chromium \
  && chmod -R a+rX /ms-playwright

COPY entrypoint.sh /entrypoint.sh
COPY server.mjs /opt/browser-node/server.mjs
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 5900 6080 9223 9224 9225

CMD ["/entrypoint.sh"]
