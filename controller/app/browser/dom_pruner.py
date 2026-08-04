"""
browser.dom_pruner — Relevance-scored interactive element pruning.

Reduces raw DOM observation payloads from ~18,000 tokens to ~500
by scoring and selecting only the N most relevant interactive elements.

Scoring factors:
  - Task keyword match in text/label/placeholder/name/id/aria
  - Element type priority (input/button/link > select/textarea > other)
  - Visibility (above fold, not hidden)
  - Recency (recently interacted elements score higher)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# Element type priority scores
_TYPE_PRIORITY: dict[str, float] = {
    "button": 10.0,
    "submit": 10.0,
    "link": 9.0,
    "input": 8.0,
    "text": 7.0,
    "password": 7.0,
    "email": 7.0,
    "search": 7.0,
    "select": 6.0,
    "textarea": 6.0,
    "checkbox": 5.0,
    "radio": 5.0,
    "file": 4.0,
    "combobox": 6.0,
    "listbox": 5.0,
}

_DEFAULT_TYPE_SCORE = 3.0

# Keyword match scoring
_KEYWORD_MATCH_SCORE = 15.0
_PARTIAL_KEYWORD_SCORE = 7.0


@dataclass
class ScoredElement:
    element: dict[str, Any]
    score: float = 0.0


def _extract_text_tokens(element: dict[str, Any]) -> list[str]:
    """Gather all text content from an element dict for keyword matching."""
    tokens: list[str] = []
    for key in ("text", "label", "placeholder", "name", "id", "aria_label", "value", "title"):
        val = element.get(key, "") or ""
        if val:
            tokens.extend(re.split(r"[\s\-_/]+", val.lower()))
    return [t for t in tokens if len(t) > 1]


def _keyword_score(element: dict[str, Any], task_keywords: list[str]) -> float:
    """Score based on task keyword overlap with element text."""
    if not task_keywords:
        return 0.0
    element_tokens = set(_extract_text_tokens(element))
    score = 0.0
    for kw in task_keywords:
        kw_lower = kw.lower()
        if kw_lower in element_tokens:
            score += _KEYWORD_MATCH_SCORE
        else:
            for tok in element_tokens:
                if kw_lower in tok or tok in kw_lower:
                    score += _PARTIAL_KEYWORD_SCORE
                    break
    return score


def _type_score(element: dict[str, Any]) -> float:
    """Score based on element type/role."""
    role = (element.get("role") or element.get("type") or element.get("tag") or "").lower()
    return _TYPE_PRIORITY.get(role, _DEFAULT_TYPE_SCORE)


def _visibility_score(element: dict[str, Any]) -> float:
    """Prefer elements that appear to be visible and above the fold."""
    if not element.get("is_visible", True):
        return -20.0  # strongly penalize hidden elements
    y = (
        element.get("y") or element.get("bounding_box", {}).get("y", 500)
        if isinstance(element.get("bounding_box"), dict)
        else 500
    )
    # Above fold (y < 800) gets a bonus, below gets a penalty
    if isinstance(y, (int, float)):
        return max(0.0, 5.0 - (y / 200.0))
    return 0.0


def _recency_score(element: dict[str, Any], recently_interacted: set[str]) -> float:
    """Boost elements that were recently interacted with."""
    elem_id = element.get("element_id") or element.get("id") or ""
    if elem_id and elem_id in recently_interacted:
        return 8.0
    return 0.0


class DOMPruner:
    """
    Prunes an observation's interactable element list to the top-N
    most relevant elements for the current task.

    Usage::

        pruner = DOMPruner(max_elements=20)
        pruned = pruner.prune(elements, task_goal="fill in the login form")
    """

    def __init__(self, max_elements: int = 20) -> None:
        self._max = max_elements
        self._recently_interacted: set[str] = set()

    def record_interaction(self, element_id: str) -> None:
        """Call after any action to boost this element in future prunes."""
        self._recently_interacted.add(element_id)
        # Keep the set bounded
        if len(self._recently_interacted) > 50:
            self._recently_interacted = set(list(self._recently_interacted)[-30:])

    def prune(
        self,
        elements: list[dict[str, Any]],
        task_goal: str = "",
        max_elements: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Score and return the top-N elements most relevant to task_goal.

        Args:
            elements:     Raw list of interactable element dicts from observe().
            task_goal:    Natural language description of what the agent is doing.
            max_elements: Override the instance default.

        Returns:
            Pruned list of element dicts, sorted by descending relevance score.
        """
        limit = max_elements if max_elements is not None else self._max
        if not elements:
            return []
        if len(elements) <= limit:
            return elements

        # Extract task keywords
        task_keywords = re.split(r"[\s,;.!?]+", task_goal.lower()) if task_goal else []
        task_keywords = [k for k in task_keywords if len(k) > 2]

        # Score each element
        scored: list[ScoredElement] = []
        for elem in elements:
            score = (
                _type_score(elem)
                + _keyword_score(elem, task_keywords)
                + _visibility_score(elem)
                + _recency_score(elem, self._recently_interacted)
            )
            scored.append(ScoredElement(element=elem, score=score))

        # Sort descending, return top-N
        scored.sort(key=lambda s: s.score, reverse=True)
        return [s.element for s in scored[:limit]]

    def prune_observation(
        self,
        observation: dict[str, Any],
        task_goal: str = "",
        max_elements: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Prune an entire observation dict in-place (returns modified copy).
        Also trims dom_outline to reduce token usage.
        """
        obs = dict(observation)
        elements = obs.get("interactable_elements") or obs.get("elements") or []
        pruned = self.prune(elements, task_goal=task_goal, max_elements=max_elements)
        obs["interactable_elements"] = pruned
        obs["elements_pruned"] = len(elements) - len(pruned)
        obs["elements_total"] = len(elements)
        return obs
