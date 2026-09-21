# Public control-plane deployment

These files publish only the human portal and the OAuth MCP gateway. They do
not publish or modify the existing pilot broker, controller, browser node,
noVNC, VNC, CDP, or Hermes stack. Nothing in this directory has been applied.

## Values the owner decides

Before the first invitation, decide:

- the display name bound to the invitation;
- an optional recovery email (entered only on the enrollment page);
- the website hostname allow-list for the owner's isolated browser stack;
- invitation lifetime (the default is one day);
- maximum simultaneously running tenant stacks (start with one);
- idle-stop timeout (the default is fifteen minutes).

The two public hostnames, certificate resolver, state paths, private network
name, and two-minute recent-authentication window are fixed by `bootstrap.sh`.
No bearer, signing key, identity key, or broker credential is supplied by the
owner.

## Ordered host runbook

Run these steps as the host orchestrator. Do not run them from an assistant or
paste any generated environment value into a chat.

1. Put the reviewed repository at the existing application root and enter it.

   ```sh
   cd /opt/auto-browser
   ```

2. Run the repository gates before touching Docker.

   ```sh
   python -m pytest approval_broker/test_app.py owner_gateway/test_app.py identity/test_app.py portal/test_app.py mcp_gateway/test_app.py deploy/tests/test_tenant_provisioner.py tenant_stacks/test_registry.py -q
   ruff check . --select E9,F,I
   python deploy/tenants/validate.py
   python deploy/public/validate.py
   ```

3. Generate the host-only environment and state directories. This refuses to
   replace an existing environment. It prints no credential and starts
   nothing.

   ```sh
   sudo ./deploy/public/bootstrap.sh
   ```

4. Ensure the already-selected internal tenant-control network exists. This
   network is never attached to Traefik.

   ```sh
   sudo docker network inspect auto-browser-tenant-control >/dev/null 2>&1 || sudo docker network create --internal auto-browser-tenant-control
   ```

5. Apply only the three-service control plane. The script validates both
   Compose templates, renders the plan, refuses published ports, and refuses
   Traefik labels on any non-public service before starting anything.

   ```sh
   sudo ./deploy/public/deploy.sh
   ```

6. While the existing private pilot has no open owner session, run the full
   boundary verification. It checks TLS, redirects, route allow-lists,
   authentication, forbidden ports, response leaks, and the existing Hermes
   private bridge. It never prints configured secret values.

   ```sh
   sudo python3 deploy/public/verify.py --env-file deploy/public/hetzner.env --env-file .env
   ```

7. Create the owner's single-use invitation. The command prints only the
   complete enrollment link; it never prints the invitation token separately
   or exposes the identity admin credential.

   ```sh
   sudo python3 deploy/public/admin.py invite --name 'OWNER_DISPLAY_NAME'
   ```

8. The owner opens that link at the public portal, enters the same display
   name and optional recovery email, opens the authenticator enrollment link,
   and types one six-digit authenticator code. Save the recovery codes shown
   once. No bearer token or configuration file is shown or typed.

9. After enrollment succeeds, provision the isolated owner stack. This command
   resolves immutable identifiers privately and prints neither identifiers nor
   broker credentials. Replace the example allow-list with reviewed website
   hostnames.

   ```sh
   sudo python3 deploy/public/admin.py provision --name 'OWNER_DISPLAY_NAME' --allowed-hosts 'example.com,www.example.com' --max-running 1 --idle-timeout-seconds 900
   ```

10. Run verification again, then sign in to the portal with a fresh
    authenticator code, open the browser, and connect an MCP client using only
    the public resource URL.

    ```sh
    sudo python3 deploy/public/verify.py --env-file deploy/public/hetzner.env --env-file .env
    ```

    ```text
    https://mcp-browser.fareeqk.com/mcp
    ```

The human portal is:

```text
https://secure-browser.fareeqk.com
```

## Normal rollback

Stop and remove only the public control-plane containers and its stack-local
network. Bind-mounted identity, portal, OAuth, and tenant data remain intact;
the private pilot and Hermes integration are untouched.

```sh
cd /opt/auto-browser
sudo docker compose --env-file deploy/public/hetzner.env -f deploy/public/compose.yml down
```

Restore the previously reviewed application revision, rerun the offline
validators, and use `deploy.sh` only when ready to re-publish.

## Emergency revocation

The fastest complete public cut-off is the same `down` command above. Do that
first. It removes both public routes because their labels live only on the two
stopped control-plane containers; it does not depend on token revocation.

For durable revocation before re-publication:

1. Keep the public control plane down.
2. Back up the protected environment as a root-only file if recovery may be
   needed, then rotate all control-plane credentials explicitly.

   ```sh
   sudo install -o root -g root -m 0600 deploy/public/hetzner.env /root/auto-browser-public-env.revoked
   sudo ./deploy/public/bootstrap.sh --rotate
   ```

3. Move the portal and gateway databases to root-only incident backups before
   restarting. This invalidates every portal session, authorization code,
   access token, and refresh token while preserving recoverable evidence.

   ```sh
   sudo install -d -o root -g root -m 0700 /root/auto-browser-incident
   sudo mv /srv/auto-browser-public/portal/portal.sqlite3 /root/auto-browser-incident/
   sudo mv /srv/auto-browser-public/mcp-gateway/gateway.sqlite3 /root/auto-browser-incident/
   ```

4. Rotate or recreate every tenant broker credential and distribute the newly
   generated portal assertion public key before bringing the service back. A
   signing-key rotation deliberately makes old tenant broker configurations
   fail closed.
5. If the incident also includes the separate Hermes pilot credential, follow
   `deploy/hermes-integration/README.md` to revoke that named bearer; do not
   expose or reuse it.
6. Re-run `deploy.sh`, complete both verification passes, and issue fresh
   invitations only after the incident boundary is understood.

## Operational notes

- The public gateway route is an exact allow-list for OAuth discovery,
  registration, authorization, token exchange, and `/mcp`; internal gateway
  endpoints, health checks, and documentation are not routed.
- Identity is on a stack-local internal network only. Portal and gateway alone
  also join Traefik and the internal tenant-broker network.
- Tenant descriptors are mode `0600` and owned by service UID `10001` so the
  portal and gateway can read broker credentials and update lifecycle metadata.
  Use `admin.py provision`; running the raw provisioner as root leaves ownership
  incompatible with the unprivileged control-plane containers.
- `verify.py` should be run before enrollment and after provisioning. A failure
  is a deployment stop, not a warning.
