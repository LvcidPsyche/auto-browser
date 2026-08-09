"""How the Codex CLI is allowed to execute on the host.

Both Codex call paths — the `cli` provider adapter and the host bridge — passed
`--dangerously-bypass-approvals-and-sandbox`, whose own help text reads "Skip all
confirmation prompts and execute commands without sandboxing. EXTREMELY
DANGEROUS. Intended solely for running in environments that are externally
sandboxed." Reported in GHSA-xmh3-cw7j-9gp5: a model decision could run arbitrary
commands on the host with no approval and no sandbox.

Nothing about the work needs it. Both paths hand Codex a screenshot and a prompt
and read back a JSON decision, in an ephemeral temp directory — Codex is being
used as a vision model, not as a shell. So the default is now `-s read-only`, and
the bypass survives only as a deliberate opt-in for people who really are running
inside an externally sandboxed environment, which is the case its own help text
describes.

Failure mode if `read-only` turns out to be too strict for someone's setup: the
Codex call fails loudly with Codex's own error, and one environment variable
restores the previous behaviour. That is the right way round — the old default
failed silently in the other direction.
"""

from __future__ import annotations

from typing import Any

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX_MODE = "read-only"

BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def codex_sandbox_flags(settings: Any) -> list[str]:
    """The sandbox portion of a `codex exec` command line."""
    return build_sandbox_flags(
        allow_host_exec=bool(getattr(settings, "codex_allow_host_exec", False)),
        sandbox_mode=str(getattr(settings, "codex_sandbox_mode", DEFAULT_SANDBOX_MODE) or DEFAULT_SANDBOX_MODE),
    )


def build_sandbox_flags(*, allow_host_exec: bool, sandbox_mode: str) -> list[str]:
    if allow_host_exec:
        return [BYPASS_FLAG]
    mode = sandbox_mode.strip() or DEFAULT_SANDBOX_MODE
    if mode not in SANDBOX_MODES:
        raise ValueError(f"CODEX_SANDBOX_MODE must be one of {', '.join(SANDBOX_MODES)} (got {mode!r})")
    return ["-s", mode]
