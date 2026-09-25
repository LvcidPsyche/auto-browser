# Multi-user browser service: decision record

Status: design only. Do not expose the current single-operator pilot to independent users.

## Confirmed requirements

- Start with 10–20 concurrent human browser sessions.
- Every human signs into their own third-party websites; assistants must not receive site passwords or MFA codes.
- Require fresh verification on the human's phone before every new browser session or takeover.
- One stable public egress IPv4 shared by all users initially; distinct per-user IPs are not currently required.
- Assistants may work across users, but each task needs the owner's explicit approval and the relevant user's fresh session verification.
- A different subdomain should be used; leave `browser.fareeqk.com` untouched. Candidate: `secure-browser.fareeqk.com`. Wildcard DNS currently resolves it to the existing host, but the secure service is not routed there yet.

## Why the current pilot cannot simply be scaled up

- Upstream Auto Browser documents its deployment as single-tenant/trusted-operator; full hostile multi-tenant isolation is outside its stated scope.
- The current stack uses `shared_browser_node`, one upstream owner bearer identity, one encryption key/data root, one visual takeover URL, and `MAX_SESSIONS=1`.
- Increasing `MAX_SESSIONS` would add concurrency, not human-user authorization or strong isolation.
- The existing 8 GB production host already runs other services. No measured safe density exists for 10–20 headed Chromium sessions. Do not change the limit on this host for external users.

## Target security boundaries

1. An identity provider enrolls each human separately. Prefer phone passkeys/WebAuthn for phishing-resistant verification; a user-owned authenticator TOTP can be the fallback. Do not store a user's website MFA seed in an agent or browser profile. SMS is not the default because of delivery cost and SIM-swap risk.
2. Session creation is a separate step-up challenge every time, even if the user still has a normal web-login cookie. A verified challenge issues a short-lived, single-use capability bound to one user and one pending browser session. A user cannot open another user's takeover link.
3. Each user's browser runtime, profile storage, downloads, encryption key, and service credential are separated. Per-session or per-user containers alone are insufficient if a shared public controller can read every user's auth state.
4. An agent can request a specific user's profile and state its purpose. The owner approves that task; the user verifies on their phone before a new browser session starts. The agent receives only a scoped session capability, never the human's login credential, upstream owner token, or raw CDP/noVNC endpoint. Completion/revocation closes the task's session.
5. The public subdomain terminates TLS at an authenticated gateway. Raw controller, VNC, noVNC, broker, Docker socket, and profile storage remain private. Every takeover link is user-bound and short-lived.
6. Profiles are encrypted at rest, backed up with their keys protected separately, and auditable. Site sessions may expire or require the site's own MFA again.

## Capacity and egress

- Measure representative target sites with 2–3 isolated users first: peak RAM, CPU, shared memory, startup time, and disk per profile. Establish a safe sessions-per-node cap from measurements, then provision capacity for 10–20 plus headroom. Do not assume the current 8 GB host can run that workload.
- All browsers on one sufficiently sized host can share its primary IPv4. If browser workers move to separate hosts but must keep the current `204.168.150.160` egress, an authenticated egress gateway/NAT on the current host is necessary; that is an additional network dependency. Resizing the current host may preserve its primary IP but affects its other production services and requires a separately approved maintenance/billing decision.
- Per-user distinct fixed IPv4 is a different requirement. It needs source-IP routing plus extra Hetzner IPs, or a static proxy per user. Neither is configured or purchased.

## Release gates

- Verify per-user login, enrollment, recovery, and phone challenge on every new session.
- Prove user A cannot list, open, observe, or download user B's profile or takeover link, even with guessed identifiers.
- Prove assistant can touch only the owner-approved tenant/profile/task and cannot bypass the user's fresh verification.
- Test concurrent sessions under realistic sites, memory pressure, process restarts, expired challenges, revoked tasks, and backup/restore.
- Only then route the new subdomain publicly. Keep the current private pilot available for the owner's own accounts during development.
