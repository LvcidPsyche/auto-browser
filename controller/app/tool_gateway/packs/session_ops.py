from __future__ import annotations

from ...tool_inputs import CdpAttachInput, ForkSessionInput, ShadowBrowseInput, ShareSessionInput
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.fork_session",
            description=(
                "Fork a session: snapshot its cookies, storage state, and current URL, "
                "then create a new independent session with that state. "
                "Useful for branching workflows or running parallel variants."
            ),
            input_model=ForkSessionInput,
            handler=gateway._fork_session,
        ),
        ToolSpec(
            name="browser.cdp_attach",
            description=(
                "Attach to an already-running Chrome instance via CDP URL "
                "(e.g. http://localhost:9222). "
                "After attaching, new sessions will use pages from that browser. "
                "This allows automation of a real browser with existing logins. "
                "Refused when more than one operator credential is configured (the attached browser "
                "would be shared by every operator): run a separate controller per operator, or set "
                "CDP_CONNECT_URL at startup. Also refused while live sessions still use the current "
                "browser: close them first. The previous browser is disconnected on success."
            ),
            input_model=CdpAttachInput,
            handler=gateway._cdp_attach,
            profiles=("full",),
            governed_kind="account_change",
        ),
        ToolSpec(
            name="browser.share_session",
            description=(
                "Create a time-limited share token for a session. "
                "Returns a signed token that grants read-only observation access. "
                "Pass the token to a teammate or use with GET /share/{token}/observe."
            ),
            input_model=ShareSessionInput,
            handler=gateway._share_session,
            profiles=("full",),
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.enable_shadow_browse",
            description=(
                "Switch a stuck session to headed (visible) mode for debugging. "
                "Creates a new headful browser window with the same state and URL. "
                "The agent can watch what's happening or a human can take over."
            ),
            input_model=ShadowBrowseInput,
            handler=gateway._enable_shadow_browse,
            profiles=("full",),
            governed_kind="write",
        ),
    ]:
        registry.register(spec)
