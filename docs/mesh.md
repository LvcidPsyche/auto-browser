# Mesh

The mesh lets one auto-browser node ask another to run a tool. It is off by default and is
inbound-only today: a node answers signed requests at `POST /mesh/receive`, and nothing in the
controller sends them yet.

## Turning it on

```
MESH_ENABLED=true
MESH_IDENTITY_DIR=/data/mesh/identity   # the node's Ed25519 key pair, created on first start
MESH_PEERS_PATH=/data/mesh/peers.json   # who may call this node, and what they may ask for
MESH_TIMESTAMP_WINDOW=30                # seconds an envelope's timestamp may differ from the clock
```

`GET /mesh/identity` returns this node's `node_id` (the SHA-256 of its public key, hex) and
`pubkey_b64`. Give those two values to the operator of any node that should call this one.

## Peers are managed in the peers file

`/mesh/receive` does not use the bearer token: a request is accepted when it is signed by a node
listed in the peers file. Adding a peer therefore grants it access to the tool gateway, so it is a
host operation. There is no API for it: edit the file `MESH_PEERS_PATH` names. The controller
re-reads it when it changes, so a new peer, a changed grant and a removal take effect without a
restart. `GET /mesh/peers` and the dashboard list the peers and cannot change them.

The file is a JSON object keyed by `node_id`; the key and the entry's own `node_id` must match:

```json
{
  "3f2c...e91a": {
    "node_id": "3f2c...e91a",
    "display_name": "build-box",
    "pubkey_b64": "base64 of the peer's 32-byte Ed25519 public key",
    "endpoint": "https://build-box.example.com",
    "grants": [
      {"capability": "tool:browser.observe"},
      {
        "capability": "tool:browser.get_html",
        "url_allowlist": ["https://*.example.com/*"],
        "require_approval": true,
        "max_invocations_per_hour": 60,
        "expires_at": 1798761600
      }
    ]
  }
}
```

- A peer can do only what a grant names. `tool:<name>` matches one tool and `tool:*` every tool;
  with no matching grant a request is rejected.
- `url_allowlist`, `require_approval`, `max_invocations_per_hour` (0 = unlimited) and
  `expires_at` (Unix time, 0 = never) are optional.
- Use the `node_id` and `pubkey_b64` the peer's own `/mesh/identity` reports. The signature is
  checked against the pinned `pubkey_b64`, so a wrong key means every request is refused.
- The controller only reads the file and never writes it. The `last_seen` that `GET /mesh/peers`
  and the dashboard show is kept in memory (it starts again from the file's value after a
  restart). Replace the file atomically (write a temporary file in the same directory, then rename
  it over the original) rather than editing in place, so the controller never reads a half-written
  file.
- In Docker, mount the directory that holds the file (the default compose file mounts `./data` at
  `/data`), not the single file. A single-file bind mount keeps pointing at the file it was made
  from, so a rename or delete on the host never reaches the container.
- Revoking an API token does not affect peers, and peers do not hold API tokens.

### What an edit does

On every `/mesh/receive` and every `GET /mesh/peers`, the controller checks the file's modification
time, size and identity, and reads the file again when any of them has changed. So:

- Removing an entry, or deleting the file, revokes that peer for every request that arrives after
  the change. A request that has already passed the sender check is not cancelled: one waiting for
  approval can still run once it is approved. For an urgent revocation, restart the controller
  after the edit.
- A missing file means no peers.
- A file that is not valid JSON (one nested too deeply to parse included), that is not an object,
  or that has any entry that is not a valid peer means no peers at all: the whole file is refused,
  not just the bad entry, until the file is valid again. The controller logs a `mesh.peers:`
  warning once for each version of the file. A half-written file is such a file.
- A file the controller cannot read (a permission error, or a path that is a directory) also means
  no peers, but there is no version of it to remember: it is retried, and logged, on every request
  until it can be read.
- An edit that leaves the modification time, size and identity all unchanged is not noticed. A
  rename over the file, as above, always changes the identity.
- After a change, `GET /mesh/peers` lists the peers the controller accepts now. An empty list
  where you expect peers means the file is missing or invalid; the controller log says which,
  except for a file that was never there.

## What `/mesh/receive` checks

In this order: the sender is in the peers file; the envelope's timestamp is inside
`MESH_TIMESTAMP_WINDOW` (a non-finite timestamp is outside it); the envelope is addressed to this
node and its Ed25519 signature verifies against the pinned key; the nonce has not been seen; then the
peer's grants decide whether the request may run. A request that fails one of the first four gets
`403` and nothing runs; one that no grant covers is answered `rejected`.

## Upgrading from a release that had `POST /mesh/peers`

`POST /mesh/peers` and `DELETE /mesh/peers/{node_id}` are removed (they answer 405 and 404), and the
dashboard no longer has a Remove button. To keep a peer you registered through them:

1. Read `MESH_PEERS_PATH` (default `/data/mesh/peers.json`). It already holds every peer that was
   registered over HTTP, in the format above, so existing peers keep working with no action.
2. Check the list. Anyone with an API token could add entries before this release, so remove
   any `node_id` you do not recognise and review each peer's `grants`.
3. From now on add, change and remove peers by editing that file, as above.

Scripts that called the removed routes must write the file instead.
