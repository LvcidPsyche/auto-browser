"""auto-browser stealth — humanization and fingerprint profiles."""

from .fingerprint import FingerprintConfig, apply_fingerprint
from .humanizer import PROFILES, Humanizer, HumanProfile

__all__ = [
    "FingerprintConfig",
    "apply_fingerprint",
    "PROFILES",
    "Humanizer",
    "HumanProfile",
]
