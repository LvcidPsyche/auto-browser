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
