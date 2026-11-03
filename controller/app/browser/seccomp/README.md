# Seccomp profile for Chromium's sandbox

`chromium-sandbox.json` is the seccomp profile that compose (`browser-node`'s `security_opt`) and the controller
(each `docker_ephemeral` browser container's `SecurityOpt`, which carries the JSON itself) apply when
`BROWSER_SANDBOX` is `auto` or `on`. With `BROWSER_SANDBOX=off` the controller applies none (Docker's default), and
compose applies none when `BROWSER_SECCOMP_PROFILE=builtin`.

It is moby's default profile plus one entry, the last in `syscalls`, whose `comment` starts with `auto-browser:`:
`chroot`, `clone` and `unshare` allowed. Without CAP_SYS_ADMIN, which these containers drop, moby's profile refuses
the user namespace Chromium's layer-1 sandbox builds and the `chroot` it makes inside it (moby allows `chroot` only
with CAP_SYS_CHROOT). The kernel still requires those capabilities in the caller's own user namespace, so the entry
lets a process act only inside a namespace it created. What it does open to code in the container is the kernel's
user-namespace code. `controller/tests/test_browser_sandbox.py` (SeccompProfileTests) checks that this entry is the only difference.

## Provenance of `moby-default.json` (byte-for-byte upstream)

| | |
|---|---|
| Source | https://raw.githubusercontent.com/moby/moby/docker-v29.9.2/vendor/github.com/moby/profiles/seccomp/default.json |
| moby tag | `docker-v29.9.2` (Docker Engine 29.9.2) |
| Module | `github.com/moby/profiles/seccomp v0.2.4` (per that tag's `vendor/modules.txt`) |
| sha256 | `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334` |

The profile replaces the engine's own default, so a stale copy can deny a syscall a newer runtime needs.
`scripts/check_chromium_freshness.py` (weekly) fails when moby's current default no longer matches
`chromium-sandbox.json` minus the auto-browser entry. To refresh: replace `moby-default.json` with the new upstream
file, update this table and the sha256 in the test, and regenerate `chromium-sandbox.json` by appending the same
entry to it.
