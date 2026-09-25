# Enrolled tenant stacks

`provisioner.py` creates one isolated Compose stack for one authenticated,
immutable `(user_id, tenant_id)` enrollment. It derives opaque deterministic
Docker names from those identifiers, stores a private descriptor under the
configured state root, and never prints credentials. It is a trusted
control-plane component, not a public enrollment endpoint.

The stack has a browser node, controller, and approval broker. Browser and
controller are only on the stack's private network. The broker also joins the
private `auto-browser-tenant-control` network with a deterministic opaque alias
for the portal/gateway; no service publishes a host port.

The portal/gateway contract is `TenantProvisioner.lookup(enrollment)`. The
returned private `TenantStackDescriptor` contains the exact bound identifiers,
`stack_key`, `broker_mcp_url`, `portal_owner_token`, and
`gateway_agent_token`. Keep this descriptor server-side; do not serialize it
to a browser or log it.

The state root must already exist, be absolute, private, and outside the
checkout. Provisioning creates two private named volumes (browser/controller
data and broker state) plus private metadata. `stop_for_idle` stops containers
without removing data. `touch_activity` records trusted portal/gateway use and
`reap_idle` enforces the configured idle timeout by stopping expired stacks
without deleting their data. `rotate_credentials` replaces the server-side
portal and gateway broker credentials, then recreates only the broker when the
stack is running. `deprovision` removes the project containers and private
network, both volumes, and the metadata directory. A configured running-stack
limit is enforced from private lifecycle metadata before start.

```sh
python3 deploy/tenants/provision.py provision \
  --state-root /srv/auto-browser-tenants \
  --user-id immutable-user-id \
  --tenant-id immutable-tenant-id \
  --allowed-hosts example.com \
  --portal-assertion-public-key AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
  --max-running 2
```

The caller must have already authenticated the immutable identifiers. Docker
administrators and host root remain trusted; Compose isolation is not a
hostile-host security boundary.

## Persistent browser profiles

With `PERSISTENT_PROFILES_ENABLED: "true"` (both services), each named login
profile is a real on-disk Chromium profile under `/data/browser-profiles/<name>`
inside browser-node, so Google/Facebook see the same device every Open.

- The controller reaches it only through browser-node's authenticated control
  API (`:9224`) and CDP relay (`:9225`); Chromium's own debugging port is
  loopback-only. Both require `TENANT_PROFILE_CONTROL_TOKEN` from `.env`,
  which the provisioner generates (and backfills into older stacks on the
  next compose call). A stack created by hand needs one line in its `.env`:
  `TENANT_PROFILE_CONTROL_TOKEN=<output of: python3 -c "import secrets; print(secrets.token_urlsafe(36))">`.
- Deleting, renaming or importing an auth profile moves its browser profile
  with it. Nothing is ever deleted automatically: replaced or deleted profiles
  go to `/data/browser-profiles/.trash/<name>--<UTC time>--<reason>`; clean
  that up by hand (`du -sh /data/browser-profiles/.trash/*` inside
  browser-node shows the sizes).
- Lock recovery is guarded by a node lease (`/data/browser-profiles/.node-lease.json`,
  heartbeat every 10s). A browser-node that finds another node's lease fresher
  than 45s (`PROFILE_NODE_LEASE_STALE_SECONDS`) refuses to launch or unlock any
  profile and logs `refusing to launch or unlock any profile`; a clean stop
  hands the lease over at once, a crash makes the next container wait up to
  the stale window. If a lease is stuck and you are certain no other
  browser-node uses the volume, start browser-node once with
  `PROFILE_NODE_LEASE_FORCE=true`, then remove it again.
- A profile directory whose owner marker is unreadable, or (for any name other
  than the remembered-login default) that has data but no marker, is moved to
  `.trash` with reason `bad-marker` / `unmarked` and a fresh profile is started.
- One live session at a time in this mode (one visible browser on the shared
  display); a second Open of the same profile returns the live session.
- Rollback: set `PERSISTENT_PROFILES_ENABLED` to `"false"` on both services and
  recreate them. Profiles stay on disk untouched for a later re-enable.
