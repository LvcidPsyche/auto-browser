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
# pytest, ruff, pip-audit and the rest of requirements-dev.txt are about 58 MB
# that a running controller never imports. `make test` and the CI job that runs
# the suite inside this image build with INSTALL_DEV_DEPS=true.
ARG INSTALL_DEV_DEPS=false

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
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
        pip install --no-cache-dir -r requirements-dev.txt; \
    elif [ "$INSTALL_DEV_DEPS" != "false" ]; then \
        echo "INSTALL_DEV_DEPS must be true or false, got '$INSTALL_DEV_DEPS'" >&2; exit 1; \
    fi

COPY app ./app
COPY tests ./tests
COPY pyproject.toml ./pyproject.toml

RUN mkdir -p /data/artifacts /data/uploads /data/auth /data/approvals /data/audit /data/db /data/jobs /data/sessions /data/browser-sessions /data/tunnels /data/cli-home \
    && chown -R 10002:10002 /data

# uvicorn never runs as root: entrypoint.sh (as root only until setpriv) runs it as the owner of /data, or as this
# user (uid 10002, never browser-node's 10001) when /data belongs to root or to 10001 (Docker Desktop bind mounts,
# named volumes, a 1.14 pre-release's data). Its home is sticky and world-writable
# because a host user's uid may run here too. CONTROLLER_RUN_AS=root keeps the old root controller.
RUN useradd --uid 10002 --user-group --create-home --home-dir /home/controller --shell /usr/sbin/nologin controller \
    && chmod 1777 /home/controller
# With the isolation overlay on Docker Desktop the controller joins group 0 (the Docker socket's group there). Nothing
# in the image is writable through group 0 alone: apt leaves the man dirs and debconf's tmp.ci g+w, so the group
# write bit comes off everything but the sticky, world-writable dirs (/tmp, /var/tmp, /run/lock).
RUN find / -xdev -group 0 -perm -g+w ! -type l ! -perm -1000 -exec chmod g-w {} +
# No setuid or setgid binaries: the controller runs no login, mount or ssh-keysign, and a process of its uid must
# have no way back up (measured before: su, passwd, mount, umount, chfn, chsh, gpasswd, newgrp, ssh-keysign setuid
# root; unix_chkpwd, chage, expiry, ssh-agent setgid).
RUN find / -xdev -perm /6000 -type f -exec chmod ug-s {} +
# 0755 whatever mode the build context gives it (a clone under umask 002 makes it group-writable): it runs as root on
# every start. Set by RUN chmod rather than a COPY flag, which only BuildKit understands.
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod 0755 /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
