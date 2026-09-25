from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.persistent_profiles import PersistentProfileClient, PersistentProfileError, normalize_profile_name


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        browser_node_host="browser-node",
        profile_control_port=9224,
        profile_control_timeout_seconds=5.0,
        persistent_profile_locale="ar-EG",
        persistent_profile_timezone="Africa/Cairo",
        persistent_profile_user_agent="",
        browser_profiles_root="/data/browser-profiles",
        profile_control_token="secret-token",
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
            "generation": "boot-7",
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
        self.assertEqual(handle.generation, "boot-7")
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
        response.json.return_value = {"cdp_endpoint": "ws://x/y", "already_open": True, "generation": "boot-1"}
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
            await client.close("owner-default", generation="boot-1")  # must not raise

    async def test_close_sends_the_profile_name(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        response = MagicMock()
        response.status_code = 200
        context, mock_client = _mock_async_client(response)

        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            await client.close("owner-default", generation="boot-4")

        body = mock_client.post.await_args.kwargs["json"]
        self.assertEqual(body, {"name": "owner-default", "generation": "boot-4"})
        self.assertEqual(
            mock_client.post.await_args.kwargs["headers"], {"Authorization": "Bearer secret-token"}
        )

    async def test_close_skips_the_request_for_an_invalid_name(self) -> None:
        settings = _settings()
        client = PersistentProfileClient(settings)
        with patch("app.persistent_profiles.httpx.AsyncClient") as mock_ctor:
            await client.close("bad name", generation="boot-1")
            mock_ctor.assert_not_called()


class PersistentProfileClientAuthTests(unittest.IsolatedAsyncioTestCase):
    """Finding 7: every control call carries the shared bearer secret, and
    nothing is attempted at all when it is not configured."""

    async def test_open_sends_the_bearer_token_and_owner(self) -> None:
        client = PersistentProfileClient(_settings())
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"cdp_endpoint": "ws://browser-node:9225/cdp/x/devtools/browser/1", "generation": "boot-3"}
        context, mock_client = _mock_async_client(response)
        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            await client.open("owner-default", owner="tenant", context_kwargs={}, storage_state=None)
        kwargs = mock_client.post.await_args.kwargs
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer secret-token"})
        self.assertEqual(kwargs["json"]["owner"], "tenant")

    async def test_no_token_refuses_before_any_request(self) -> None:
        client = PersistentProfileClient(_settings(profile_control_token=""))
        with patch("app.persistent_profiles.httpx.AsyncClient") as mock_ctor:
            with self.assertRaises(PersistentProfileError):
                await client.open("owner-default", context_kwargs={}, storage_state=None)
            with self.assertRaises(PersistentProfileError):
                await client.trash("owner-default", reason="deleted", owner=None)
            with self.assertRaises(PersistentProfileError):
                await client.rename("a", "b", owner=None)
            self.assertFalse(await client.close("owner-default", generation="boot-1"))
            mock_ctor.assert_not_called()

    async def test_trash_and_rename_send_owner_and_surface_status(self) -> None:
        client = PersistentProfileClient(_settings())
        response = MagicMock()
        response.status_code = 403
        response.json.return_value = {"error": "profile directory belongs to a different owner"}
        context, mock_client = _mock_async_client(response)
        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            with self.assertRaises(PersistentProfileError) as caught:
                await client.trash("owner-default", reason="deleted", owner="tenant")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            mock_client.post.await_args.kwargs["json"],
            {"name": "owner-default", "reason": "deleted", "owner": "tenant"},
        )

        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"renamed": True}
        context, mock_client = _mock_async_client(ok)
        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            result = await client.rename("old-name", "new-name", owner=None)
        self.assertEqual(result, {"renamed": True})
        self.assertEqual(mock_client.post.await_args.args[0], "http://browser-node:9224/profiles/rename")


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_without_a_generation_is_refused(self) -> None:
        client = PersistentProfileClient(_settings())
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"cdp_endpoint": "ws://x/y"}
        context, _mock_client = _mock_async_client(response)
        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            with self.assertRaises(PersistentProfileError):
                await client.open("owner-default", context_kwargs={}, storage_state=None)

    async def test_numeric_generation_is_rejected(self) -> None:
        client = PersistentProfileClient(_settings())
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"cdp_endpoint": "ws://x/y", "generation": 3}
        context, _mock_client = _mock_async_client(response)
        with patch("app.persistent_profiles.httpx.AsyncClient", return_value=context):
            with self.assertRaises(PersistentProfileError):
                await client.open("owner-default", context_kwargs={}, storage_state=None)

    async def test_close_without_a_generation_sends_nothing(self) -> None:
        client = PersistentProfileClient(_settings())
        with patch("app.persistent_profiles.httpx.AsyncClient") as mock_ctor:
            self.assertFalse(await client.close("owner-default", generation=None))
            mock_ctor.assert_not_called()


class RuntimeAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_sends_the_token_and_does_not_apply_client_defaults(self) -> None:
        from app.browser.services.runtime import BrowserRuntimeService
        from app.persistent_profiles import PersistentProfileHandle

        context = object()
        browser = MagicMock()
        browser.contexts = [context]
        manager = MagicMock()
        manager.playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        manager.persistent_profiles = PersistentProfileClient(_settings())
        handle = PersistentProfileHandle(
            name="owner-default",
            cdp_endpoint="ws://browser-node:9225/cdp/owner-default/devtools/browser/1",
            already_open=False,
            seeded=False,
            was_empty=True,
        )
        attachment = await BrowserRuntimeService(manager).attach_persistent_context(handle)
        manager.playwright.chromium.connect_over_cdp.assert_awaited_once_with(
            handle.cdp_endpoint,
            headers={"Authorization": "Bearer secret-token"},
            no_defaults=True,
        )
        self.assertIs(attachment.context, context)

    async def test_attach_with_no_context_disconnects_but_does_not_release(self) -> None:
        from app.browser.services.runtime import BrowserRuntimeService
        from app.persistent_profiles import PersistentProfileHandle

        browser = MagicMock()
        browser.contexts = []
        browser.close = AsyncMock()
        manager = MagicMock()
        manager.playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        manager.persistent_profiles = PersistentProfileClient(_settings())
        manager.persistent_profiles.close = AsyncMock()
        handle = PersistentProfileHandle("owner-default", "ws://x/y", False, False, True)
        with self.assertRaises(RuntimeError):
            await BrowserRuntimeService(manager).attach_persistent_context(handle)
        browser.close.assert_awaited_once()
        manager.persistent_profiles.close.assert_not_awaited()


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
