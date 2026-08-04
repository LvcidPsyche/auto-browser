from __future__ import annotations

from ...tool_inputs import GetCookiesInput, GetStorageInput, SetCookiesInput, SetStorageInput
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.get_cookies",
            description=("Get all cookies for the current session context. Optionally filter by URL(s)."),
            input_model=GetCookiesInput,
            handler=gateway._get_cookies,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.set_cookies",
            description=(
                "Set one or more cookies in the current session context. "
                "Each cookie dict must have at minimum: name, value, domain."
            ),
            input_model=SetCookiesInput,
            handler=gateway._set_cookies,
            profiles=("full",),
            governed_kind="account_change",
        ),
        ToolSpec(
            name="browser.get_local_storage",
            description=("Read a key (or all keys) from localStorage or sessionStorage in the current page context."),
            input_model=GetStorageInput,
            handler=gateway._get_local_storage,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.set_local_storage",
            description=("Write a key-value pair to localStorage or sessionStorage in the current page context."),
            input_model=SetStorageInput,
            handler=gateway._set_local_storage,
            profiles=("full",),
            governed_kind="account_change",
        ),
    ]:
        registry.register(spec)
