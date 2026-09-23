FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

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

WORKDIR /opt/browser-node

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
  && npx playwright install chromium

COPY entrypoint.sh /entrypoint.sh
COPY server.mjs /opt/browser-node/server.mjs
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 5900 6080 9223

CMD ["/entrypoint.sh"]
