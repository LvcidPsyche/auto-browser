#!/bin/sh
# Start the Auto Browser HTTP server in the background, then run the stdio bridge.
# Used by the root Dockerfile for Glama MCP inspection.
#
# SESSION_ISOLATION_MODE=glama_inspect skips the browser-node connection attempt
# at startup (ensure_browser retries for 60s by default, which exceeds the
# healthz timeout and prevents the server from becoming ready in time).

# Activate uv venv if present (Glama builds use uv venv /opt/venv)
if [ -f /opt/venv/bin/activate ]; then
    . /opt/venv/bin/activate
fi

# The app module lives under controller/ — move there so Python can find it
cd /app/controller || exit 1

# Launch uvicorn in the background; skip browser-node connection for inspection.
#
# API_BIND_SCOPE=loopback declares what --host 127.0.0.1 does: nothing outside
# this container can reach the API, and the bridge below is its only client.
# The controller cannot see uvicorn's --host flag (only API_BIND_HOST,
# UVICORN_HOST or HOST in the environment), so without this it assumed the
# fail-safe `exposed` scope and refused to start with no API_BEARER_TOKEN —
# which left the stdio bridge talking to nothing since 1.7.0.
API_BIND_SCOPE=loopback \
SESSION_ISOLATION_MODE=glama_inspect \
    uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Wait up to 60s for the server to become healthy
i=0
while [ $i -lt 60 ]; do
    if curl -sf http://127.0.0.1:8000/healthz > /dev/null 2>&1; then
        break
    fi
    sleep 1
    i=$((i + 1))
done

# Run the stdio ↔ HTTP MCP bridge (replaces this shell process)
exec python -m app.mcp_stdio --base-url http://127.0.0.1:8000/mcp
