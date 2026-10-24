"""The instructions an MCP client puts in the model's context when it connects.

They are the one place to say how the tools fit together, so they may name
only tools this controller serves: a sentence that names a tool outside the
selection (MCP_TOOL_PROFILE / MCP_TOOL_PACKS) is left out. Which tools a
sentence needs is read from the sentence itself, so a new sentence cannot
forget to declare one.
"""

from __future__ import annotations

import re
from collections.abc import Collection

_TOOL_NAME = re.compile(r"\b(?:browser|harness)\.[a-z_]+")

# The reading sentence lists what is served, so lean (no downloads) still gets it.
_READERS = (
    "browser.get_html with text_only=true (paged)",
    "browser.find_elements with a query",
    "browser.read_download for downloaded files",
)
_READING = object()  # where the reading sentence goes

_SENTENCES = (
    "Auto Browser drives a real, supervised Chromium session.",
    "Typical loop: browser.observe (it creates a session when none is live) to get interactables and their "
    "element_id values, then browser.execute_action with an element_id; its result reports what changed and "
    "the page after the action.",
    "Without vision, use observe preset='text'; to look at the page, browser.screenshot returns an image.",
    _READING,
    "Navigation is limited to the hosts the operator allows.",
    "Some actions return status approval_required with an approval_id: ask the operator to approve it (their "
    "dashboard is at /dashboard on this controller), then retry with approval_id.",
    "For logins, CAPTCHAs or anything a person should do, call browser.request_human_takeover.",
)


def _names_served(text: str, served: Collection[str]) -> bool:
    return all(name in served for name in _TOOL_NAME.findall(text))


def _english_list(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def server_instructions(served: Collection[str]) -> str:
    sentences = []
    for sentence in _SENTENCES:
        if sentence is _READING:
            readers = [reader for reader in _READERS if _names_served(reader, served)]
            if readers:
                sentences.append(f"To read content, use {_english_list(readers)}.")
        elif _names_served(sentence, served):
            sentences.append(sentence)
    return " ".join(sentences)


# The text with every sentence: what the curated and full profiles send.
SERVER_INSTRUCTIONS = server_instructions(
    {name for text in (*_SENTENCES, *_READERS) if isinstance(text, str) for name in _TOOL_NAME.findall(text)}
)
