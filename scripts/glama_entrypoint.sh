#!/bin/sh
# Start the Auto Browser HTTP server in the background, then run the stdio bridge.
# Used by the root Dockerfile for Glama MCP inspection.

# Activate uv venv if present (Glama builds use uv venv /opt/venv)
if [ -f /opt/venv/bin/activate ]; then
    . /opt/venv/bin/activate
fi

# Launch uvicorn in the background
uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Wait up to 30s for the server to become healthy
i=0
while [ $i -lt 30 ]; do
    if curl -sf http://127.0.0.1:8000/healthz > /dev/null 2>&1; then
        break
    fi
    sleep 1
    i=$((i + 1))
done

# Run the stdio ↔ HTTP MCP bridge (replaces this shell process)
exec python -m app.mcp_stdio --base-url http://127.0.0.1:8000/mcp
