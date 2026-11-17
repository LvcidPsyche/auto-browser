"""Drive the root (Glama) image as an MCP client over stdio: initialize, then tools/list.

Glama's inspection runs the image and lists its tools; 1.15.0 to 1.18.1 failed at every start, and no check ran the
image. This fails unless the image answers both on a stdout that carries MCP messages only, within the entrypoint's
60-second wait and a margin.

Usage: python3 scripts/ci/glama_mcp_probe.py IMAGE
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

ANSWER_SECONDS = 150
MIN_TOOLS = 1


def main(image: str) -> int:
    proc = subprocess.Popen(
        ["docker", "run", "-i", "--rm", "--network", "none", image],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    timer = threading.Timer(ANSWER_SECONDS, proc.kill)  # a server that never answers ends the run, not the job
    timer.start()
    try:

        def send(message: dict) -> None:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        def answer(request_id: int) -> dict:
            for line in proc.stdout:
                message = json.loads(line)  # anything but an MCP message on stdout fails the run here
                if message.get("id") == request_id:
                    if "result" not in message:
                        raise SystemExit(f"FAIL: request {request_id} was answered with {json.dumps(message)[:500]}")
                    return message["result"]
            raise SystemExit(f"FAIL: no answer to request {request_id}; the image exited {proc.wait()}")

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "ci", "version": "0"},
                },
            }
        )
        print("initialize:", json.dumps(answer(1)["serverInfo"]))
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [tool["name"] for tool in answer(2)["tools"]]
        print(f"tools/list: {len(tools)} tools")
        if len(tools) < MIN_TOOLS:
            raise SystemExit("FAIL: tools/list returned no tools")
        return 0
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
