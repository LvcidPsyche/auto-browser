# Private single-browser Hetzner pilot

This deployment is deliberately private. The controller, noVNC, raw VNC, and approval broker bind only to the server's loopback interface. Do not publish ports 18000, 18001, 16080, or 15900 in a firewall or reverse proxy. Do not give an agent the upstream owner/root token or shell access to this host.

## Where the browser runs

The browser node, controller, and MCP approval broker all run on the Hetzner server, not on the owner's computer. The existing private owner UI currently needs an SSH tunnel only to view/control the server browser from a remote device. It is **not** a local browser runtime. A mobile-friendly public owner portal is not deployed; raw noVNC and broker must not be exposed as a shortcut. A safe HTTPS ingress needs authentication for both the owner UI and visual browser.

## Current private owner access

Run `deploy/open-owner.ps1`. It opens SSH local forwards and the owner page. The owner token is copied to the clipboard; paste it into the prompt, then clear the clipboard. SSH host-key checking is strict. The visual browser is available via the owner page on the same tunnel. Only one live session is enabled; finish and close it before starting another.

Before the first session, use the owner page to set up a phone authenticator app: copy the displayed private key into an RFC 6238 TOTP app and confirm its first six-digit code. This is an app-generated code, not SMS or push. The owner token and a fresh phone code are required **only to open a new browser session**. The assistant never receives or supplies this phone code and does not prompt for it per task. Each code can only be used once. Keep the authenticator seed private and backed up securely; losing it currently requires manual operator recovery on the host. Do not enroll it through an agent. Enrollment is incomplete until the owner enters the first code.

Open a session from the owner page with a fresh phone code. Optionally select a saved auth profile; otherwise start blank. Open the visual browser running on the server, complete site logins and site MFA yourself, and save the named auth profile. A saved storage state may hold cookies for multiple sites present in that browser context; a site may still expire or revoke its login. Closing the session does not delete the saved profile. Reopening it requires another phone code. There is no fixed session duration, but leaving a logged-in session open allows authorized assistants to keep using it.

## Agent access

Named agent bearer credentials are stored in the protected server `.env`. The assistant authenticates to the private broker, calls `browser.request_access` with a purpose, and receives a capability bound to the **already open** owner session. No phone code or per-task owner approval is needed. The assistant cannot create, reopen, select a different saved profile for, or close the browser session. `browser.complete` ends only that assistant capability, not the owner's session. Owner closure revokes all capabilities, and the next session again needs a fresh phone code. A broker restart loses its trusted session binding and fails closed; the owner must close the orphaned controller session and open a new one with a fresh code. The MCP endpoint exposes only a narrow set of browser tools, not raw CDP, controller APIs, profile export, or noVNC.

For an assistant on the server, connect it through a private container network to `http://approval-broker:18001/mcp` with only its own bearer credential. Do not configure the owner token in an assistant, publish the broker's raw port, or grant Docker/host-root access. The present Hermes container is on a different Docker network; it has a configurable HTTP MCP client, but its network attachment and Auto Browser MCP settings have not yet been installed.

The browser's URL allowlist is in `.env` as `ALLOWED_HOSTS`. To add a site for agent navigation, the owner must review the domain, update this setting, and restart the controller. The owner may type a URL manually in the visual browser, but automation is limited to the allowlist. No proxy is configured. The browser container's observed IPv4 egress was `204.168.150.160`, the host's primary IP. It stays the same while that primary IP remains assigned to this server; it is not a floating/reserved IP for replacement servers.

## Security limits

- SSH key plus private tunnel plus owner token protect the current private UI; owner token plus an enrolled TOTP app are required to start each new browser session. This is not phone push or SMS. The private tunnel, not a public domain, is the current ingress.
- A live visual browser session remains open until closed; the TOTP check gates its creation, not every subsequent click or reopening the noVNC tab. Shared-owner tokens or leaked SSH access defeat this boundary.
- Broker session binding and grants are in memory. A restart fails closed but may leave an upstream browser session that the owner must close.
- Revocation blocks new operations; an already in-flight browser operation can finish before the lock is released.
- Any process with Docker or host-root access can bypass the broker. This is a trusted single-tenant installation, not a hardened multi-tenant service.
- Distinct profile names in this shared browser-node pilot do not provide strong isolation between independent human users. Do not onboard separate users until each has a separately authenticated browser runtime and isolated state/key, with resource capacity validated.
- Open source does not remove the need to patch Chromium, the controller, and operating system, or to protect encrypted backups and the encryption key.
