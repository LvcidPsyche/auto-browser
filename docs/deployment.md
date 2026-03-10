# Auto Browser Deployment Guide

This is the recommended deployment shape for a **single-tenant, trusted-operator** production install.

## Recommended topology

- Auto Browser controller + browser node on one private host
- Front the controller and takeover UI with **Cloudflare Access** or **Tailscale**
- Keep published ports bound to `127.0.0.1`
- Use `docker_ephemeral` session isolation if the operator may touch multiple accounts/workflows

## Required production settings

At minimum:

```env
APP_ENV=production
API_BEARER_TOKEN=<strong-random-secret>
REQUIRE_OPERATOR_ID=true
AUTH_STATE_ENCRYPTION_KEY=<44-char-fernet-key>
REQUIRE_AUTH_STATE_ENCRYPTION=true
REQUEST_RATE_LIMIT_ENABLED=true
METRICS_ENABLED=true
```

Strongly recommended:

```env
SESSION_ISOLATION_MODE=docker_ephemeral
MAX_SESSIONS=1
ALLOWED_HOSTS=<your-real-allowlist>
STATE_DB_PATH=/data/db/operator.db
ARTIFACT_RETENTION_HOURS=168
UPLOAD_RETENTION_HOURS=168
AUTH_RETENTION_HOURS=168
```

## Provider authentication choices

You now have two viable ways to authenticate model providers:

### Option A — API keys

Use:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

This is still the cleanest option for CI and broadly automated installs.

### Option B — subscription-backed CLIs

Use this on a trusted private box if you already run:

- `codex` via ChatGPT/Codex login
- `claude` via Claude Code login/subscription
- `gemini` via Gemini CLI login

Set:

```env
OPENAI_AUTH_MODE=cli
CLAUDE_AUTH_MODE=cli
GEMINI_AUTH_MODE=cli
CLI_HOME=/data/cli-home
```

Then copy the signed-in CLI state into the mounted data directory:

```bash
mkdir -p data/cli-home
rsync -a ~/.codex data/cli-home/.codex
cp ~/.claude.json data/cli-home/.claude.json
rsync -a ~/.claude data/cli-home/.claude
rsync -a ~/.gemini data/cli-home/.gemini
```

Treat `data/cli-home` like a password vault. Never commit it.

In `APP_ENV=production`, startup now fails fast if:
- any `*_AUTH_MODE` value is not `api` or `cli`
- a provider is set to `cli` mode but its CLI binary is missing
- `CLI_HOME` is set but the expected auth-state files are missing for that CLI

## Generate an auth-state encryption key

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

## Local/private production run

```bash
cp .env.example .env
# edit .env with the required production values above

docker compose -f docker-compose.yml -f docker-compose.isolation.yml up -d --build
```

## Health and readiness checks

```bash
curl -fsS http://127.0.0.1:8000/healthz | jq
curl -fsS http://127.0.0.1:8000/readyz | jq
curl -fsS http://127.0.0.1:8000/metrics | head
curl -fsS http://127.0.0.1:8000/maintenance/status | jq
```

## Gateway recommendations

Use **one** of:

- **Cloudflare Access** in front of the controller + noVNC paths
- **Tailscale** and keep the whole stack private
- the included reverse-SSH path for bastion-style access when direct reachability is not available

Do **not** expose raw controller or noVNC ports directly to the public internet.

## Backups

Back up at least:

- `/data/db/` if using SQLite
- `/data/sessions/`
- `/data/jobs/`
- `/data/auth/` if you intentionally keep reusable auth state
- `/data/audit/`

## Cleanup

The controller can now prune stale artifacts/uploads/auth-state automatically.

Manual run:

```bash
curl -s http://127.0.0.1:8000/maintenance/cleanup \
  -X POST \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H 'X-Operator-Id: ops' | jq
```

## Credential handoff checklist

Before live debugging, gather:

- OpenAI / Anthropic / Gemini API keys, or populated CLI auth caches under `data/cli-home`
- gateway credentials (Cloudflare Access or Tailscale)
- bastion SSH details if using reverse tunnels
- operator identity convention (`X-Operator-Id` values)
- allowlisted target hosts/domains

## First live-debug session

1. Bring the stack up privately
2. Verify `/readyz`
3. Verify `/metrics`
4. Create one session against a non-sensitive site
5. Verify observe/click/type flow
6. Add real creds
7. Test one real target workflow with human takeover ready
