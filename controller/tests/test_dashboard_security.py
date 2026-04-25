from __future__ import annotations

import unittest

from app.routes.extensions import _DASHBOARD_HTML


class DashboardSecurityTests(unittest.TestCase):
    def test_dashboard_dynamic_tables_do_not_use_inner_html(self) -> None:
        self.assertIn("appendCell(row, s.name)", _DASHBOARD_HTML)
        self.assertIn("document.getElementById('stat-sessions').textContent", _DASHBOARD_HTML)
        self.assertNotIn("innerHTML", _DASHBOARD_HTML)
        self.assertNotIn("onclick=\"removePeer", _DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
