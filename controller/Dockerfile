FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The codex, claude and gemini CLIs back the *_AUTH_MODE=cli providers
# (subscription logins) and need Node.js; together they are about 750 MB that
# an MCP-only or API-key deployment never runs. Build with
# INSTALL_AGENT_CLIS=true to include them (docker-compose reads it from .env;
# docker-compose.host-subscriptions.yml sets it).
ARG INSTALL_AGENT_CLIS=false
ARG CODEX_CLI_VERSION=0.114.0
ARG CLAUDE_CLI_VERSION=2.1.72
ARG GEMINI_CLI_VERSION=0.32.1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends autossh openssh-client tesseract-ocr tesseract-ocr-eng \
    && if [ "$INSTALL_AGENT_CLIS" = "true" ]; then \
        apt-get install -y --no-install-recommends nodejs npm \
        && npm install -g \
            @openai/codex@${CODEX_CLI_VERSION} \
            @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
            @google/gemini-cli@${GEMINI_CLI_VERSION} \
        && npm cache clean --force; \
    elif [ "$INSTALL_AGENT_CLIS" != "false" ]; then \
        echo "INSTALL_AGENT_CLIS must be true or false, got '$INSTALL_AGENT_CLIS'" >&2; exit 1; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
COPY pyproject.toml ./pyproject.toml

RUN mkdir -p /data/artifacts /data/uploads /data/auth /data/approvals /data/audit /data/db /data/jobs /data/sessions /data/browser-sessions /data/tunnels /data/cli-home

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
