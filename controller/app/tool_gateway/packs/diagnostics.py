from __future__ import annotations

from ...tool_inputs import (
    EmptyInput,
    ExportScriptInput,
    GetNetworkLogInput,
    GetRemoteAccessInput,
    ReadinessCheckInput,
    VerifyWitnessInput,
)
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.get_remote_access",
            description="Read current remote-access metadata for takeover/API forwarding.",
            input_model=GetRemoteAccessInput,
            handler=gateway._get_remote_access,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.readiness_check",
            description=(
                "Run a deployment readiness check. Returns pass/warn/fail for encryption, "
                "operator identity, bearer token, session isolation, Witness audit, "
                "host allowlist, PII scrubbing, and upload approval. "
                "Pass mode='confidential' for stricter checks."
            ),
            input_model=ReadinessCheckInput,
            handler=gateway._readiness_check,
        ),
        ToolSpec(
            name="browser.get_network_log",
            description=(
                "Return captured HTTP request/response entries for a session. "
                "Filtered by method (GET/POST/...) or URL substring. "
                "All sensitive headers and bodies are automatically PII-scrubbed."
            ),
            input_model=GetNetworkLogInput,
            handler=gateway._get_network_log,
        ),
        ToolSpec(
            name="browser.verify_witness",
            description=(
                "Verify the integrity of a session's Witness receipt hash chain. "
                "Walks every receipt, recomputes the chain, and reports the first "
                "divergent receipt if the log was altered, reordered, or truncated."
            ),
            input_model=VerifyWitnessInput,
            handler=gateway._verify_witness,
            read_only_hint=True,
        ),
        ToolSpec(
            name="browser.export_script",
            description=(
                "Export the current session's recorded actions as a runnable "
                "Playwright Python script. Returns the script as a string "
                "that can be saved to a .py file and run standalone."
            ),
            input_model=ExportScriptInput,
            handler=gateway._export_script,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.pii_scrubber_status",
            description=(
                "Return the current PII scrubber configuration: which patterns "
                "are active, which layers are enabled, and the replacement string."
            ),
            input_model=EmptyInput,
            handler=gateway._pii_scrubber_status,
            profiles=("full",),
        ),
    ]:
        registry.register(spec)
