# Five private human-browser pilots (not an enrollment service)

This directory defines five *independent Compose projects*, each containing one
controller and one headed browser node. Each gets a distinct project network,
bind-mounted data root, Fernet auth-state key, share-link signing secret, and
named bearer credential. All five use the host's existing outbound NAT/IP if
run on that host. There are **no published host ports**. The `pilot` profile
means ordinary `docker compose up` starts nothing. Provisioning creates files
only; it does not build, start, route, or publish a service.

**Do not invite humans or agents yet.** There is no phone challenge, user
enrollment, user-bound takeover authorization, gateway, or scoped agent broker
in this template. In particular, noVNC itself has no authentication. A bearer
credential alone does not satisfy the fresh phone-verification requirement.
`TAKEOVER_URL` is intentionally not reachable through the host; never solve
that by publishing the raw noVNC/controller ports. A separate private gateway
with per-session verified phone step-up and user-bound authorization is a
release prerequisite, not a setting in this file.

## Prepare, after approval to touch the host

Keep the state root outside the checkout and existing production data. On the
Linux host, with a trusted administrative account, choose actual allowed site
hosts and provision once:

```sh
python3 deploy/tenants/provision.py \
  --state-root /srv/auto-browser-tenants \
  --allowed-hosts example.com
python3 deploy/tenants/validate.py
```

The example domain is a placeholder; choose the actual approved sites. The
script refuses to overwrite any of the five directories. Protect and back up
the secret `.env` separately from encrypted profiles; losing its Fernet key
can make stored auth state unrecoverable. Treat the host's root and Docker
administrators as fully trusted: they can access every tenant, regardless of
Compose networks or file modes. Do not commit `.env` or copy it into tickets.

To check the rendered configuration on the host without showing secrets in
terminal output:

```sh
docker compose \
  --env-file /srv/auto-browser-tenants/tenant01/.env \
  -f deploy/tenants/compose.yml \
  --profile pilot config --quiet
```

Never run `docker compose config` without `--quiet` against real credentials:
its output contains all resolved environment values.

## Staged operation, not a five-user launch

The current shared 8 GiB production server has roughly 4.1 GiB available RAM,
already uses swap, and runs other workloads. This template caps each browser
at 1.5 GiB and controller at 0.5 GiB (plus host/daemon overhead). Five stacks
could require more memory than the machine has, even before active sites.
Begin with **one** pilot only after a separately approved capacity window and
the private phone-verification path. Do not activate additional projects until
realistic peak RAM, swap, CPU, process, disk, and restart behavior are measured
and a safe concurrency limit is established. An OOM under these caps is an
expected failure mode; do not simply lift the caps on this shared host.

Once those gates are met, an operator can explicitly activate only the chosen
project, from the repository root:

```sh
docker compose \
  --env-file /srv/auto-browser-tenants/tenant01/.env \
  -f deploy/tenants/compose.yml \
  --profile pilot up -d --build
```

Stop that project with `down` **without** `-v`; bind-mounted tenant data is
intentionally retained. Do not attach a tenant's controller or browser to
another tenant network, share a Docker socket, reuse an `.env`, or use the
existing shared pilot's Compose overrides. The browser node's Playwright and
noVNC endpoints are reachable by its own controller on its private project
network, but this is container separation on one trusted Docker host, not a
hostile-tenant security boundary. Browser navigation is outbound over the
project's distinct NAT network to the same host public IP.

Before any external rollout, prove cross-tenant denial (profiles, downloads,
API, visual takeover), fresh phone verification for every session, revocation,
private ingress behavior, and capacity under representative websites. Docker
Compose's successful render is not evidence for those properties.
