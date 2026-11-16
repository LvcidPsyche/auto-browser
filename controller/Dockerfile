FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The codex, claude and gemini CLIs back the *_AUTH_MODE=cli providers
# (subscription logins), and the last two need Node.js; together they are about 750 MB that
# an MCP-only or API-key deployment never runs. Build with
# INSTALL_AGENT_CLIS=true to include them (docker-compose reads it from .env;
# docker-compose.host-subscriptions.yml sets it).
ARG INSTALL_AGENT_CLIS=false
# codex is the glibc build from its own GitHub release, not the npm package: that one's binary is the musl build, whose
# sandbox helper opens / without O_PATH (Rust std drops the flag on musl), which the controller's Landlock launcher
# refuses, so Codex ran none of its model's commands. The sums are of rust-v${CODEX_CLI_VERSION}'s
# codex-<arch>-unknown-linux-gnu.tar.gz and of the binary in it, which the release's .sigstore bundle signs. A bump
# changes the version and all four sums in one commit (AGENTS.md says how to verify them).
ARG CODEX_CLI_VERSION=0.114.0
ARG CODEX_CLI_TARBALL_SHA256_AMD64=f18294d4da6756e9e5ad4dc62b3030b7a75a94e5e5022f3bfa4a21a7d3c52ae0
ARG CODEX_CLI_BINARY_SHA256_AMD64=8bae8fa33887a8d829815fa0af85887cbac3658ac1b6147a4b1d3d54c5e015de
ARG CODEX_CLI_TARBALL_SHA256_ARM64=c923ffd32c50b8d8f4c5f39e8623c8afab90ee984c65f3c73709b82cf63150bb
ARG CODEX_CLI_BINARY_SHA256_ARM64=97c76fae7574d5db734685fe269885bfa2334070651a6ecdfca4024352f54ef0
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
        apt-get install -y --no-install-recommends nodejs npm curl \
        && npm install -g \
            @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
            @google/gemini-cli@${GEMINI_CLI_VERSION} \
        && npm cache clean --force \
        && case "$(dpkg --print-architecture)" in \
            amd64) codex_arch=x86_64 \
                codex_tar_sum="$CODEX_CLI_TARBALL_SHA256_AMD64" codex_bin_sum="$CODEX_CLI_BINARY_SHA256_AMD64" ;; \
            arm64) codex_arch=aarch64 \
                codex_tar_sum="$CODEX_CLI_TARBALL_SHA256_ARM64" codex_bin_sum="$CODEX_CLI_BINARY_SHA256_ARM64" ;; \
            *) echo "No codex-cli build is pinned for $(dpkg --print-architecture): build with INSTALL_AGENT_CLIS=false" >&2; \
                exit 1 ;; \
        esac \
        && mkdir /tmp/codex-cli \
        && curl -fsSL -o /tmp/codex-cli/codex.tar.gz \
            "https://github.com/openai/codex/releases/download/rust-v${CODEX_CLI_VERSION}/codex-${codex_arch}-unknown-linux-gnu.tar.gz" \
        && echo "${codex_tar_sum}  /tmp/codex-cli/codex.tar.gz" | sha256sum -c - \
        && tar -xzf /tmp/codex-cli/codex.tar.gz -C /tmp/codex-cli \
        && echo "${codex_bin_sum}  /tmp/codex-cli/codex-${codex_arch}-unknown-linux-gnu" | sha256sum -c - \
        && install -m 0755 -o root -g root "/tmp/codex-cli/codex-${codex_arch}-unknown-linux-gnu" /usr/local/bin/codex \
        && rm -rf /tmp/codex-cli; \
    elif [ "$INSTALL_AGENT_CLIS" != "false" ]; then \
        echo "INSTALL_AGENT_CLIS must be true or false, got '$INSTALL_AGENT_CLIS'" >&2; exit 1; \
    fi \
    && rm -rf /var/lib/apt/lists/*

# constraints.txt pins every package the two requirements files pull in, at releases at least 72 hours old (R5C-14):
# when either file changes, generate it again with the command in its header, with a cutoff 72 hours before then.
COPY requirements.txt requirements-dev.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt \
    && if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
        pip install --no-cache-dir -r requirements-dev.txt -c constraints.txt; \
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
# --timeout-graceful-shutdown: at a stop uvicorn waits for the responses in flight before it runs the app's shutdown
# (sessions closed, containers and tunnels removed), with no limit unless one is given, and an event stream a client
# holds open (GET /mcp, GET /sessions/{id}/events) never ends by itself: the cleanup never ran, and Docker killed the
# process when its grace was over. 5 seconds lets an ordinary call in flight finish and leaves the cleanup 55 of the
# 60 that docker-compose.yml's stop_grace_period gives; keep the two true of each other (a test reads both).
# --lifespan on: the app's middleware is built at its first call, the lifespan's startup, and under uvicorn's default
# (auto) a failure there, such as a malformed CONTROLLER_ALLOWED_HOSTS, read as an app without a lifespan: uvicorn
# served, the app's startup never ran, and every request got a 500. With it the start fails and uvicorn exits.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5", "--lifespan", "on"]
