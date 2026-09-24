from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.persistent_profiles import PersistentProfileClient, normalize_profile_name


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        browser_node_host="browser-node",
        profile_control_port=9224,
        profile_control_timeout_seconds=5.0,
        persistent_profile_locale="ar-EG",
        persistent_profile_timezone="Africa/Cairo",
        persistent_profile_user_agent="",
        browser_profiles_root="/data/browser-profiles",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _mock_async_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, client


class NormalizeProfileNameTests(unittest.TestCase):
    def test_accepts_ordinary_names(self) -> None:
        self.assertEqual(normalize_profile_name(" owner-default "), "owner-default")
        self.assertEqual(normalize_profile_name("nihad.google_v2"), "nihad.google_v2")

    def test_rejects_empty_or_path_like_names(self) -> None:
        with self.assertRaises(ValueError):
            normalize_profile_name("")
        with self.assertRaises(ValueError):
            normalize_profile_name("../../etc/passwd")
        with self.assertRaises(ValueError):
            normalize_profile_name("has spaces")


class PersistentProfileClientOpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_sends_pinned_locale_and_returns_handle(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "cdp_endpoint": "ws://browser-node:1234/devtools/browser/abc",
            "already_open": False,
            "seeded": True,
            "was_empty": True,
        }
        context, mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            handle = await client.open(
                "owner-default",
                context_kwargs={"viewport": {"width": 1280, "height": 800}},
                storage_state={"cookies": []},
            )

        self.assertEqual(handle.cdp_endpoint, "ws://browser-node:1234/devtools/browser/abc")
        self.assertTrue(handle.seeded)
        self.assertTrue(handle.was_empty)
        self.assertFalse(handle.already_open)
        body = mock_client.post.await_args.kwargs["json"]
        self.assertEqual(body["name"], "owner-default")
        self.assertEqual(body["locale"], "ar-EG")
        self.assertEqual(body["timezone_id"], "Africa/Cairo")
        self.assertEqual(body["storage_state"], {"cookies": []})
        self.assertNotIn("user_agent", body)  # no override configured -> real Chromium UA stands

    async def test_open_prefers_explicit_locale_over_pinned_default(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"cdp_endpoint": "ws://x/y", "already_open": True}
        context, mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            await client.open("owner-default", context_kwargs={"locale": "en-GB"}, storage_state=None)

        body = mock_client.post.await_args.kwargs["json"]
        self.assertEqual(body["locale"], "en-GB")

    async def test_open_raises_on_missing_cdp_endpoint(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {}
        context, _mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            with self.assertRaises(RuntimeError):
                await client.open("owner-default", context_kwargs={}, storage_state=None)

    async def test_open_raises_on_error_status(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 400
        response.text = "invalid profile name"
        context, _mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            with self.assertRaises(RuntimeError):
                await client.open("owner-default", context_kwargs={}, storage_state=None)

    async def test_open_rejects_invalid_profile_name_before_any_request(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        with patch("app.persistent_profiles.httpx.AsyncClient") as mock_ctor:
            with self.assertRaises(ValueError):
                await client.open("../etc", context_kwargs={}, storage_state=None)
            mock_ctor.assert_not_called()


class PersistentProfileClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_is_best_effort_and_never_raises(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        with patch("app.persistent_profiles.httpx.AsyncClient", side_effect=RuntimeError("network down")):
            await client.close("owner-default")  # must not raise

    async def test_close_sends_the_profile_name(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 200
        context, mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            await client.close("owner-default")

        body = mock_client.post.await_args.kwargs["json"]
        self.assertEqual(body, {"name": "owner-default"})

    async def test_close_skips_the_request_for_an_invalid_name(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        with patch("app.persistent_profiles.httpx.AsyncClient") as mock_ctor:
            await client.close("bad name")
            mock_ctor.assert_not_called()


class ProfileDiskUsageTests(unittest.TestCase):
    def test_returns_none_for_missing_directory(self) -> None:
        settings = _settings(browser_profiles_root="/does/not/exist")
        client = PersistentProfileClient(settings)
        self.assertIsNone(client.profile_disk_usage_bytes("owner-default"))

    def test_returns_none_for_invalid_name(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        self.assertIsNone(client.profile_disk_usage_bytes("../etc"))

    def test_sums_file_sizes_under_the_profile_root(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "owner-default"
            (root / "Default").mkdir(parents=True)
            (root / "Default" / "Cookies").write_bytes(b"x" * 10)
            (root / "Default" / "History").write_bytes(b"y" * 5)
            settings = _settings(browser_profiles_root=tmp)
            client = PersistentProfileClient(settings)
            self.assertEqual(client.profile_disk_usage_bytes("owner-default"), 15)


if __name__ == "__main__":
    unittest.main()
