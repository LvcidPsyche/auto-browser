FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG CODEX_CLI_VERSION=0.114.0
ARG CLAUDE_CLI_VERSION=2.1.72
ARG GEMINI_CLI_VERSION=0.32.1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends autossh openssh-client nodejs npm tesseract-ocr tesseract-ocr-eng \
    && npm install -g \
        @openai/codex@${CODEX_CLI_VERSION} \
        @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
        @google/gemini-cli@${GEMINI_CLI_VERSION} \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
COPY pyproject.toml ./pyproject.toml

RUN mkdir -p /data/artifacts /data/uploads /data/auth /data/approvals /data/audit /data/db /data/jobs /data/sessions /data/browser-sessions /data/tunnels /data/cli-home

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
