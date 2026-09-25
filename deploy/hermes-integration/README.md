# Hermes to Auto Browser private MCP integration

Applied to the existing Hetzner server on 2026-09-21. The dedicated internal
Docker network, durable compose attachments, named Hermes bearer, and MCP
configuration are in place. `hermes mcp test auto_browser` discovered the broker
tools, and `verify-on-hetzner.py` confirmed initialization, tool listing, and
denial when the owner has not opened a TOTP-gated session.

## Intended boundary

```text
Hermes (one named bearer) -> private Docker network -> approval-broker /mcp
                                                        -> owner session
                                                        -> Auto Browser
```

Hermes receives only a new `hermes` entry in `BROKER_AGENT_TOKENS`. It never
receives `BROKER_OWNER_TOKEN`, the controller owner token, a TOTP secret/code,
Docker access, or a published broker port. The broker works only with an owner
session that is already open. The owner supplies TOTP when opening that
session; Hermes must never be asked for a phone code.

## Durable compose changes already applied

Docker `network connect` disappears after container recreation. The operator
who controls both Dokploy deployments must make these persistent compose
changes before treating the integration as durable:

```yaml
# Auto Browser deployment
services:
  approval-broker:
    networks:
      - default
      - hermes_browser_private
networks:
  hermes_browser_private:
    external: true
    name: hermes-browser-private
```

```yaml
# Hermes deployment
services:
  hermes:
    networks:
      - dokploy-network
      - hermes_browser_private
networks:
  hermes_browser_private:
    external: true
    name: hermes-browser-private
```

Create the network once before deploying either change:

```sh
docker network create --driver bridge --internal hermes-browser-private
```

Do not attach the broker to `dokploy-network`, and do not publish a broker
port. The dedicated bridge is internal; the broker keeps its normal Auto
Browser network for controller access.

## Hermes configuration persistence

The existing Hermes startup preserves unknown `mcp_servers` entries. The
browser MCP configuration lives in Hermes's persistent `/opt/data/config.yaml`.
The server-side apply script creates the bearer only if absent and updates this
configuration without printing it. No deployment environment bearer is needed.

The tool list exposes only the broker workflow, not raw controller, CDP, profile,
or noVNC access.

## Safe sequence

1. Run `check-on-hetzner.sh` as root on the server. It is read-only and never
   prints a token.
2. Confirm no owner browser session is open. A broker restart deliberately
   fails closed and removes its in-memory session binding.
3. Make and deploy the two durable compose changes.
4. Run `apply-on-hetzner.sh --apply` once. It creates or reuses the named
   bearer without printing it, recreates only the broker, and configures Hermes.
5. The owner opens one fresh TOTP-gated session. Hermes then calls
   `browser.request_access` for each task and `browser.complete` at its end.

## Validation and rollback

The check verifies bearer shape, loopback-only broker exposure, internal shared
network membership, DNS, and Hermes MCP configuration without revealing a
secret. An actual Hermes request should fail before an owner session exists and
work only after the owner opens it.

Rollback: unset `mcp_servers.auto_browser`, redeploy Hermes without the two
variables and network attachment, then redeploy Auto Browser without the broker
attachment. After no containers use it, remove `hermes-browser-private`.
Revoking Hermes alone means deleting only its named bearer, restarting the
broker while no owner session is open, then opening a new TOTP-gated session.

## Remaining owner setup

The owner has not enrolled TOTP or opened a session. The remote HTTPS portal is
not publicly deployed because Cloudflare Access app/policy management is not
available to the existing API token, and the exact allowlisted owner email is
not yet known. No external ChatGPT/Claude public MCP connector is deployed.
