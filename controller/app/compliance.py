from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PRESETS: dict[str, dict[str, Any]] = {
    "strict": {
        "require_auth_state_encryption": True,
        "require_operator_id": True,
        "pii_scrub_enabled": True,
        "pii_scrub_screenshot": True,
        "pii_scrub_network": True,
        "pii_scrub_console": True,
        "require_approval_for_uploads": True,
        "witness_enabled": True,
        "auth_state_max_age_hours": 4.0,
        "session_isolation_mode": "docker_ephemeral",
    },
    "balanced": {
        "require_operator_id": True,
        "pii_scrub_enabled": True,
        "pii_scrub_network": True,
        "require_approval_for_uploads": True,
        "witness_enabled": True,
        "auth_state_max_age_hours": 24.0,
    },
}

_DEPRECATED_ALIASES: dict[str, str] = {
    "HIPAA": "strict",
    "PCI-DSS": "strict",
    "SOC2": "balanced",
    "GDPR": "balanced",
}

VALID_PRESETS = set(_PRESETS)
VALID_TEMPLATES = VALID_PRESETS | set(_DEPRECATED_ALIASES)


def apply_compliance_template(settings: Any, template_name: str) -> dict[str, Any]:
    raw = template_name.strip()
    normalized = raw if raw in _DEPRECATED_ALIASES else raw.lower()
    if normalized in _DEPRECATED_ALIASES:
        target = _DEPRECATED_ALIASES[normalized]
        logger.warning(
            "compliance: %r is a deprecated alias and will be removed in a future release; use %r",
            normalized,
            target,
        )
        normalized = target
    if normalized not in _PRESETS:
        raise ValueError(f"Unknown compliance preset: {raw!r}. Valid options: {sorted(_PRESETS)}")

    overrides = _PRESETS[normalized]
    applied: dict[str, Any] = {}
    for attribute, value in overrides.items():
        current = getattr(settings, attribute, None)
        if current != value:
            setattr(settings, attribute, value)
            logger.info("compliance[%s]: %s = %r (was %r)", normalized, attribute, value, current)
        applied[attribute] = value
    return applied


def write_compliance_manifest(*, template_name: str, overrides: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "template": template_name,
        "applied_overrides": overrides,
        "note": (
            "This manifest records the compliance preset applied at startup. Settings were overridden as shown above."
        ),
    }
    tmp_path = output_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp_path.replace(output_path)
    logger.info("compliance manifest written to %s", output_path)
