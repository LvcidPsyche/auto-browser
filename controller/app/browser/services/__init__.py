from __future__ import annotations

from .actions import BrowserActionService
from .approvals import BrowserApprovalService
from .auth_profiles import BrowserAuthProfileService
from .bot_challenge import BrowserBotChallengeService
from .diagnostics import BrowserDiagnosticsService
from .dialogs import BrowserDialogService
from .observation import BrowserObservationService
from .remote_access import BrowserRemoteAccessService
from .runtime import BrowserRuntimeService
from .sessions import BrowserSessionService
from .tabs import BrowserTabService
from .takeover import BrowserTakeoverService
from .uploads import BrowserUploadService
from .witness import BrowserWitnessService

__all__ = [
    "BrowserActionService",
    "BrowserApprovalService",
    "BrowserAuthProfileService",
    "BrowserBotChallengeService",
    "BrowserDiagnosticsService",
    "BrowserDialogService",
    "BrowserObservationService",
    "BrowserRemoteAccessService",
    "BrowserRuntimeService",
    "BrowserSessionService",
    "BrowserTabService",
    "BrowserTakeoverService",
    "BrowserUploadService",
    "BrowserWitnessService",
]
