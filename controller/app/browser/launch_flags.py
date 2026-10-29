"""Launch arguments for the browsers the controller starts itself (the shadow launch).

Playwright launches Chromium with ``--disable-features=<its list>``, and that list turns
ThirdPartyStoragePartitioning off. Without partitioning, a refused host's iframe on an allowed page registers a
service worker that then serves a top-level navigation to that host, or a popup's first navigation, with no
request the pre-request gate sees (step 0, m3). Chromium keeps the last ``--disable-features`` switch, so
appending Playwright's list without that one feature turns partitioning back on and keeps everything else
Playwright disables (measured, step 0). This is real Chrome's default (decision 9).

browser-node/server.mjs carries the same list for browser-node and docker_ephemeral's containers.
tests/test_playwright_disabled_features.py fails when either copy differs from the installed Playwright
driver's list minus ThirdPartyStoragePartitioning, so a Playwright upgrade cannot drift them silently.
"""

from __future__ import annotations

# Playwright 1.62's disabledFeatures (driver/package/lib/coreBundle.js), in its order, without
# ThirdPartyStoragePartitioning.
PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING = (
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "BoundaryEventDispatchTracksNodeRemoval",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "HttpsUpgrades",
    "LensOverlay",
    "MediaRouter",
    "PaintHolding",
    "BlockOriginHeaderModificationOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
    "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion",
)
STORAGE_PARTITIONING_ON = "--disable-features=" + ",".join(PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING)
