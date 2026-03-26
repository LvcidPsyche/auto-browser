# Root Dockerfile — used by Glama for MCP server inspection.
# Starts the Auto Browser HTTP server and exposes it via the stdio MCP bridge,
# so any stdio-capable MCP client (or Glama's inspector) can enumerate tools.
#
# For production use, see docker-compose.yml which wires up the full stack
# (controller + browser-node + optional reverse-SSH tunnel).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTO_BROWSER_BASE_URL=http://127.0.0.1:8000/mcp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng curl \
    && rm -rf /var/lib/apt/lists/*

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/app ./app

RUN mkdir -p /data/artifacts /data/uploads /data/auth /data/approvals \
             /data/audit /data/db /data/jobs /data/sessions /data/browser-sessions \
             /data/tunnels /data/cli-home

# Startup script: launch HTTP server then bridge stdio → MCP HTTP
COPY scripts/glama_entrypoint.sh /glama_entrypoint.sh
RUN chmod +x /glama_entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/glama_entrypoint.sh"]
