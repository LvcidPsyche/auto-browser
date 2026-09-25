# Session Isolation Audit

**Date:** 2026-05-17
**Scope:** Per-session boundary analysis for cookies, storage, service workers, takeover surface, and auth state across the two supported isolation modes.
**Prompted by:** External review claim that "shared Playwright + noVNC takeover + reused auth profiles creates session poisoning paths."

## TL;DR

The external claim is **partially correct**. Browser-level state (cookies, localStorage, sessionStorage, IndexedDB, cache, service workers) is properly isolated per session in both modes because each session calls `browser.new_context()`. The real shared surface in the default mode is the **takeover plane** (one X display, one noVNC) and the **browser process** (shared OS-level state like DNS cache and process memory). Auth state files are *copied* into new contexts, not referenced by handle, so cross-session contamination through auth profiles does not happen.

Two real concerns remain. One is acknowledged in `docs/architecture.md` as a POC constraint. The other deserves explicit documentation but is not a bug.

## Modes under test

| Mode | Browser process | Browser context | Takeover surface |
|------|-----------------|-----------------|-------------------|
| `shared_browser_node` (default) | Shared (one Chromium) | Isolated per session via `new_context()` | Shared (one X display, one noVNC) |
| `docker_ephemeral` | Isolated (one container per session) | Isolated per session | Per-session noVNC, optional per-session reverse-SSH |

## What's isolated (verified)

- **Cookies, localStorage, sessionStorage, IndexedDB, cache, service workers** — all per `BrowserContext`. `BrowserSessionService.create()` in `controller/app/browser/services/sessions.py` calls `browser.new_context(**context_kwargs)` for every session, which is Playwright's documented boundary for all these stores.
- **Auth state on disk** — `BrowserSessionService.create()` calls `auth_state.prepare_for_context(source_path)` which creates a *copy* of the storage state for the new context. Mutations stay in the per-session copy. Source profile is not aliased.
- **Per-session artifacts** — each session gets its own `artifact_dir`, `auth_dir`, `upload_dir` under `/data/sessions/{session_id}/`. Traces, screenshots, and uploads do not cross sessions.
- **Network inspector state** — attached per-page in `BrowserSessionService.create()`, not shared across sessions.

## What's shared in the default mode (real concerns)

### 1. Takeover surface (acknowledged in architecture.md, POC constraint)

The default `shared_browser_node` runs one Chromium with one X display and one noVNC. A human takeover sees every visible window on that desktop. If two sessions are live, a takeover operator looking at session A may incidentally see session B's pages.

**Mitigation already in repo:** `docker_ephemeral` mode gives each session its own browser container with its own noVNC port pair. The session record built in `BrowserSessionService.create()` carries `takeover_url`, `shared_takeover_surface`, and `shared_browser_process` per session so operators can see which mode they're in.

**Recommendation:** Document this explicitly in `README.md` near the takeover section, not just in `architecture.md`. Many users will skim the README and miss the POC caveat. *(Done: the README's Security and Compliance section now says when to choose `docker_ephemeral`.)*

### 2. Browser process kernel state (low risk, worth documenting)

In `shared_browser_node`, sessions share Chromium's process: DNS cache, font cache, ICU tables, V8 isolate pools. Playwright's context boundary handles user-data isolation but not these. Threat model for most users is benign — but for a security-conscious operator running auth flows for distinct identities on the same node, the right answer is `docker_ephemeral`, not `shared_browser_node`.

**Recommendation:** Add a short "Choosing an isolation mode" section to `docs/architecture.md` or a new `docs/isolation-modes.md` so this tradeoff is explicit instead of folklore.

## What's NOT a real concern (despite the external claim)

- **Cookies bleeding between sessions** — `new_context()` is Playwright's documented isolation boundary. Verified in code.
- **Service workers persisting** — service workers are scoped to a context's origin storage; new contexts get fresh service-worker registries. Verified.
- **Auth profile reuse "leaking"** — `prepare_for_context()` copies the storage state file; the source is read-only relative to the session. Mutations land in the per-session copy.
- **Reverse-SSH tunnel sharing** — `BrowserSessionService.create()` calls `_maybe_provision_session_tunnel(session)` which can broker a per-session reverse-SSH for isolated takeover ports. Shared tunnels are not blindly reused for isolated sessions per the architecture doc.

## Recommendations (in order of leverage)

1. **README clarity, not code change.** *(Done.)* Add a short "Choosing isolation" paragraph in the README near the deployment section. Two-mode table from this audit + one-sentence guidance: "Use `docker_ephemeral` when sessions belong to different identities or trust domains."
2. **No behavioral change required for the externally-raised concern.** Browser-level state isolation is already correct. Publishing this audit *is* the win — it converts a perception risk ("they don't really isolate") into a documented, defensible posture.
3. **Long-term:** consider making `docker_ephemeral` the default once the per-session reverse-SSH path has a few weeks of soak. The POC-era default of `shared_browser_node` is a memory/CPU optimization for solo developers; production users almost always want the stronger boundary.

## Test coverage gaps spotted during audit

- No explicit regression test asserts that `new_context()` is called per session (it's the architectural guarantee; one cheap test would lock it in).
- No test confirms that `prepare_for_context()` returns a path different from the source for auth-profile flows.
- No end-to-end test covers "two simultaneous sessions, one writes a cookie, the other does not see it."

A `good first issue` could be: add these three regression tests to `controller/tests/test_sessions.py`.

## Conclusion

The architecture is more sound than the external claim implied. The honest answer is: **browser-level state is isolated, takeover surface and process state are shared in the default POC mode, and `docker_ephemeral` exists precisely to upgrade those when needed.** Publishing this audit closes the perception gap without requiring a code change.
