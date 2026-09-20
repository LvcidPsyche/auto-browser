# Remote owner portal: staged gateway only

The gateway is designed for `https://secure-browser.fareeqk.com`. This directory does **not** create a DNS record, Cloudflare Access application or policy, Traefik route, or public port. Do not publish the hostname until the account-level Cloudflare Access application and its single-owner allow policy are active and verified. A DNS-only token is insufficient for that step.

Cloudflare Access requirements:

1. Create a self-hosted Access application for the exact hostname above. Configure an **Allow** policy for only the owner's exact email address using the email one-time PIN identity provider. Avoid broad domain, everyone, bypass, service-token, and application-path exemptions. Set a suitably short session duration. Cloudflare may require account-level Access application and policy edit permissions; a zone DNS token cannot create these. The administrator must provide the Access team hostname, application AUD tag, and exact allowlisted email as the environment values below. This gateway validates the signed JWT independently, so a direct-origin request without a valid Access assertion still fails.
2. Set `CF_ACCESS_TEAM_DOMAIN` to the hostname of the form `<team>.cloudflareaccess.com` (not a URL), `CF_ACCESS_AUD` to that application audience, and `OWNER_EMAIL` to the exact allowlisted address. Set `BROKER_OWNER_TOKEN` to the existing broker owner token via private deployment secrets, never a browser variable or Traefik label. The gateway fetches signing keys from Cloudflare Access over TLS, verifies RS256 signature, issuer, audience, expiry, issued-at, and email on **every HTTP and WebSocket request**. A key fetch failure fails closed.
3. Attach only `owner-gateway:18002` to the existing Traefik/Dokploy ingress network and route only the exact hostname to it over HTTPS. Preserve the `Cf-Access-Jwt-Assertion` header and WebSocket upgrades. Do not route the broker, controller, noVNC, Playwright, or raw VNC ports directly. Keep existing loopback-only host port publishes unchanged. If the Traefik network is separate from the stack network, add the gateway to both without adding the private services to the public network. Restrict direct host ingress to Cloudflare IPs where practical, but do not rely on that instead of JWT validation.
4. Before exposing DNS/route, confirm the Access login works for the owner and fails for another email. Then verify `/owner` and `/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify` from a phone. The portal's email OTP protects entry; the existing owner authenticator TOTP still gates opening each new browser session. No gateway-side TOTP is added.

Run only after those prerequisites are complete (paths are relative to repository root):

```sh
docker compose -f docker-compose.yml -f deploy/hetzner.compose.yml -f deploy/owner-access/compose.yml --profile owner-access up -d --build owner-gateway
```

Route contract:

```text
GET /owner
GET /owner.js
GET /owner/totp
POST /owner/totp/setup
POST /owner/totp/activate
GET, POST /owner/sessions
DELETE /owner/sessions/{id}
GET /owner/auth-profiles
POST /owner/sessions/{id}/auth-profiles
GET /requests
POST /requests/{id}/deny
POST /requests/{id}/revoke
GET, HEAD /vnc/*
WebSocket /vnc/websockify
```

All other public routes are denied, including `/mcp`, agent request creation, controller APIs, the broker's private `/owner/visual-access` guard, and raw VNC. The gateway injects the broker owner bearer only on allowed broker requests. Before any noVNC file or WebSocket is served, the gateway privately asks the broker whether its process-local, TOTP-opened owner session is still active. The WebSocket binds to that session ID, rechecks before every client control frame, and checks periodically; a closed or replaced session terminates access. Broker errors fail closed. The broker UI JavaScript is adapted server-side to remove its manual owner-token prompt and point noVNC to the protected path; if the broker JavaScript changes, the gateway returns an incompatibility error rather than serving an unadapted page.

The gateway is not a general reverse proxy and is not a Cloudflare Access replacement. Cloudflare's policy must be in place before publishing it. Keep Cloudflare application changes and production deployment as separately approved operational steps.
