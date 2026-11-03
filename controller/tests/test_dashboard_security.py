from __future__ import annotations

import unittest

from app.routes.extensions import _DASHBOARD_HTML


class DashboardSecurityTests(unittest.TestCase):
    def test_dashboard_dynamic_tables_do_not_use_inner_html(self) -> None:
        self.assertIn("appendCell(row, s.name)", _DASHBOARD_HTML)
        self.assertIn("document.getElementById('stat-sessions').textContent", _DASHBOARD_HTML)
        self.assertIn("agent-jobs-tbody", _DASHBOARD_HTML)
        self.assertIn("resumeAgentJob", _DASHBOARD_HTML)
        self.assertIn("discardAgentJob", _DASHBOARD_HTML)
        self.assertIn("cancelAgentJob", _DASHBOARD_HTML)
        self.assertNotIn("innerHTML", _DASHBOARD_HTML)
        self.assertNotIn('onclick="removePeer', _DASHBOARD_HTML)

    def test_stored_token_is_trimmed_before_it_becomes_the_authorization_header(self) -> None:
        self.assertIn("(sessionStorage.getItem('ab_token') || '').trim()", _DASHBOARD_HTML)

    def test_the_token_prompt_says_where_the_token_is(self) -> None:
        # A controller with no API_BEARER_TOKEN generates one in data/api-token; blank no longer works.
        self.assertIn(
            "Enter the API token (API_BEARER_TOKEN, or data/api-token in your auto-browser folder on a local install):",
            _DASHBOARD_HTML,
        )
        self.assertNotIn("no-auth", _DASHBOARD_HTML)
        self.assertIn("window.location.hash", _DASHBOARD_HTML)  # #token= still works

    def test_replay_view_present_and_renders_safely(self) -> None:
        # Panel + wiring exist.
        self.assertIn('id="replay"', _DASHBOARD_HTML)
        self.assertIn("async function loadReplay()", _DASHBOARD_HTML)
        self.assertIn("getElementById('load-replay').addEventListener('click', loadReplay)", _DASHBOARD_HTML)
        # Reads from existing run/approval artifacts.
        self.assertIn("/agent/jobs/", _DASHBOARD_HTML)
        self.assertIn("/approvals", _DASHBOARD_HTML)
        # Renders untrusted run data via text nodes / safe helpers, never innerHTML.
        self.assertIn("appendCell(row, decision.action)", _DASHBOARD_HTML)
        self.assertIn("statusBadge(result.status)", _DASHBOARD_HTML)
        self.assertIn("safeHttpUrl(v)", _DASHBOARD_HTML)

    def test_auth_wizard_present_and_renders_safely(self) -> None:
        # Panel + four-step wiring exist.
        self.assertIn('id="auth-wizard"', _DASHBOARD_HTML)
        self.assertIn("async function wizardStart()", _DASHBOARD_HTML)
        self.assertIn("async function wizardSave()", _DASHBOARD_HTML)
        self.assertIn("async function wizardReopen()", _DASHBOARD_HTML)
        self.assertIn("getElementById('wizard-start').addEventListener('click', wizardStart)", _DASHBOARD_HTML)
        # Reuses existing auth-profile + session endpoints (save named profile, reopen).
        self.assertIn("/auth-profiles", _DASHBOARD_HTML)
        self.assertIn("auth_profile: name", _DASHBOARD_HTML)
        # Untrusted profile names / takeover URLs handled safely (text nodes, safeHttpUrl,
        # option value set as a property — never interpolated into HTML).
        self.assertIn("safeHttpUrl(session.takeover_url)", _DASHBOARD_HTML)
        self.assertIn("opt.textContent = name", _DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
