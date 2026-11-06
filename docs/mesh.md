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
MESH_MAX_BODY_BYTES=1048576             # largest /mesh/receive body in bytes; over it gets 413 unparsed
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
        "max_invocations_per_hour": 60,
        "expires_at": 1798761600
      }
    ]
  }
}
```

- A peer can do only what a grant names. `tool:<name>` matches one tool and `tool:*` every tool;
  with no matching grant a request is rejected. When several grants match, the most specific one
  applies, whatever its position in the file: an exact capability beats a wildcard, a longer
  wildcard prefix beats a shorter one, and equal specificity goes to the entry listed first. A
  `tool:*` grant therefore cannot shadow a stricter `tool:browser.get_html` grant listed after it.
- `url_allowlist` is checked at call time against the URL arguments of the request (`url`,
  `start_url`, `urls` and `cdp_url`, at any depth). It does not constrain where the page then
  redirects or navigates, or what a later call does, so it limits the first URL a peer can name,
  not the sites a session ends up on.
- `url_allowlist`, `max_invocations_per_hour` (0 = unlimited) and `expires_at` (Unix time, 0 = never)
  are optional.
- `require_approval` is deprecated. Nothing can approve a mesh request (inbound delegation has no approval
  handler), so a grant that sets it to `true` always answers `approval_required` and its capability never
  runs. The field is still read, so an existing file stays valid, and loading a grant that sets it logs a
  `mesh.peers:` warning. To allow a capability, leave the field out; to refuse one, leave out its grant.
- Use the `node_id` and `pubkey_b64` the peer's own `/mesh/identity` reports, exactly as it reports
  them. A `node_id` is the SHA-256 of the 32-byte key, in lower-case hex, and an entry where it is
  not (or whose key is not that base64) makes the whole file invalid, as below. The signature is
  checked against the pinned `pubkey_b64`.
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

Before any of that the body is read with a cap: `MESH_MAX_BODY_BYTES` (1 MiB by default). A request with no
`Content-Length`, one that declares more, or one that sends more than the cap gets `413` and is not parsed;
the route takes no bearer token, so this is what stops an unauthenticated caller from making the controller
buffer an arbitrary body. A body sent without `Content-Length` (chunked) is refused however small, by design: a
peer must send `Content-Length`, which auto-browser's own mesh client does.

In this order: the sender is in the peers file; the envelope's timestamp is inside
`MESH_TIMESTAMP_WINDOW` (a non-finite timestamp is outside it); the envelope is addressed to this
node and its Ed25519 signature verifies against the pinned key; the nonce has not been seen; then the
peer's grants decide whether the request may run. A request that fails one of the first four gets
`403` and nothing runs; one that no grant covers is answered `rejected`.

Replay protection: the timestamp window bounds how long an envelope can be replayed, and a seen nonce is
remembered until the latest time this node's clock has shown is more than one window past the envelope's
timestamp, then forgotten (a clock stepped back does not free it early). The cache remembers at most 10,000 nonces
at once, and nothing it remembers is dropped before it expires: when it is full of nonces still inside their
window, a new envelope is refused with `503` ("mesh replay cache is full; retry later") and not recorded, and a
retry passes once older nonces expire. The limit: a nonce is recorded once the envelope's signature checks out,
before the request is decoded or granted, so registered peers that get 10,000 signed envelopes through inside a
window fill the cache, and every peer is refused (`503`) until they expire (`/mesh/receive` takes no bearer token,
so the request rate limit, on by default at 120 requests a minute, counts it per client address, and one address
alone cannot fill it unless that limit is raised or off). The timestamp window is checked against that same latest
time, never the raw clock, so a clock stepped back cannot put an envelope whose nonce is already forgotten inside
the window again. A peer's reply to a delegation this node sent is checked against the same clock and nonce cache,
and refused unless it names the request's id. The cost: after a large backward step of the clock, fresh envelopes
are refused (`403`) until the clock catches up, for about the step minus the window. Nonces are kept in memory
only, so a restart empties them; to close that gap the controller also refuses any envelope stamped before the
moment it started or within one window after it (`403`). The last process accepted envelopes stamped up to one
window ahead of its clock, so this covers everything it could have run. The cost is about one window
(`MESH_TIMESTAMP_WINDOW`, 30 seconds by default) of refusals after each start, which a sender's retry with a new
envelope passes once that window is over. Replay protection therefore holds across a restart as long as the clock
did not step back across it: the high-water mark is kept in memory only, so a controller restarted with its clock
set back by more than a window can accept again, once its clock nears their stamps, the envelopes the last process
ran stamped later than the step minus one window before the restart (all of them once the step reaches how long it
ran, or, after an upgrade from 1.15.0, which refused nothing at startup, how long it ran plus two windows), and
one set back by less, the envelopes it accepted stamped more than the window minus the step after the restart. The
check also assumes the last process ran with no larger `MESH_TIMESTAMP_WINDOW`: after lowering it, a restart is
safe once the old window has passed since the last envelope was accepted (or restart first with the old value, and
lower it after that long).

## Upgrading from a release that had `POST /mesh/peers`

`POST /mesh/peers` and `DELETE /mesh/peers/{node_id}` are removed (they answer 405 and 404), and the
dashboard no longer has a Remove button. To keep a peer you registered through them:

1. Read `MESH_PEERS_PATH` (default `/data/mesh/peers.json`). It already holds every peer that was
   registered over HTTP, in the format above.
2. Check the list. Anyone with an API token could add entries before this release, so remove
   any `node_id` you do not recognise and review each peer's `grants`. They could also replace the
   key of a peer you do recognise, so compare each `pubkey_b64` with what that peer's own
   `/mesh/identity` reports.
3. Check that every entry is valid for this release, because one invalid entry turns every peer
   away: each `node_id` must be the SHA-256 of its key, in lower-case hex, and each key must be
   written exactly as `/mesh/identity` reports it. Earlier releases did not check this, so an entry
   with a name of your choosing as its `node_id`, or a key with stray characters, used to work and
   now does not. A peer that runs auto-browser signs as its own `/mesh/identity` `node_id`, so
   register it under that. The `mesh.peers:` warning names the first entry that fails and why; a
   mismatch says the pair does not fit together, not who changed it.
4. From now on add, change and remove peers by editing that file, as above.

Scripts that called the removed routes must write the file instead.
