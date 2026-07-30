from __future__ import annotations

from ...tool_inputs import (
    DragDropInput,
    EvalJsInput,
    FindElementsInput,
    GetPageHtmlInput,
    SetViewportInput,
    VisionFindInput,
    WaitForSelectorInput,
)
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.eval_js",
            description=(
                "Execute a JavaScript expression in the current page context "
                "and return the result. Use for DOM queries, value extraction, "
                "or lightweight scripting that has no dedicated tool."
            ),
            input_model=EvalJsInput,
            handler=gateway._eval_js,
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.wait_for_selector",
            description=(
                "Wait for a CSS selector to reach a specific state "
                "(visible, hidden, attached, detached). "
                "Returns when the condition is met or raises on timeout."
            ),
            input_model=WaitForSelectorInput,
            handler=gateway._wait_for_selector,
        ),
        ToolSpec(
            name="browser.get_html",
            description=(
                "Get the HTML source of the current page. "
                "Set text_only=true to strip tags and return plain text. "
                "Set full_page=false (default) for visible viewport only."
            ),
            input_model=GetPageHtmlInput,
            handler=gateway._get_html,
        ),
        ToolSpec(
            name="browser.find_elements",
            description=(
                "Find elements either by CSS selector or by a text/regex query across the "
                "page's text content. selector mode returns matching elements' text, href, "
                "value, bounding box, and visibility -- useful before clicking or scraping "
                "multiple items. query mode (set query, optionally regex and context) returns "
                "the matched text plus surrounding context for each hit -- a cheap way to "
                "locate a specific string on the page without a full observe."
            ),
            input_model=FindElementsInput,
            handler=gateway._find_elements,
        ),
        ToolSpec(
            name="browser.drag_drop",
            description=(
                "Drag from one element or coordinate to another. "
                "Provide source_selector OR (source_x, source_y), "
                "and target_selector OR (target_x, target_y)."
            ),
            input_model=DragDropInput,
            handler=gateway._drag_drop,
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.set_viewport",
            description="Resize the browser viewport to the specified width and height.",
            input_model=SetViewportInput,
            handler=gateway._set_viewport,
        ),
        ToolSpec(
            name="browser.find_by_vision",
            description=(
                "Use Claude Vision to find an element from a natural language description. "
                "Returns (x, y) coordinates you can pass to browser.execute_action click. "
                "Use when CSS selectors fail or the element has no reliable text anchor."
            ),
            input_model=VisionFindInput,
            handler=gateway._find_by_vision,
        ),
    ]:
        registry.register(spec)
