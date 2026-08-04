"""Security regression tests for auth-profile path handling and archive import.

`BrowserAuthProfileService` is the module that reads, writes and imports stored
browser auth state — cookies and session tokens. Its containment guards were
correct but effectively untested (the module sat at 22% coverage, and the only
"import" test in the suite called `tarfile.extractall` directly rather than the
service), so nothing would catch a refactor that quietly weakened one.

These tests pin the *rejection* behaviour, which is the part that matters: a
guard that stops rejecting still passes every happy-path test. Each test names
the attack it prevents.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.browser.services import BrowserAuthProfileService

Service = BrowserAuthProfileService


class _RecordingAudit:
    """Captures audit events so tests can assert credential moves are recorded."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append(self, **kwargs) -> None:
        self.events.append(kwargs)


def _service(auth_root: Path) -> BrowserAuthProfileService:
    """Service backed by a throwaway auth root.

    `audit` is real (recording) rather than absent: export and import move
    credentials off and onto the box, and both now record that they happened.
    """
    return BrowserAuthProfileService(
        SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(auth_root), auth_state_encryption_key=None),
            audit=_RecordingAudit(),
        )
    )


def _tar_with(members: list[tuple[tarfile.TarInfo, bytes | None]], dest: Path) -> None:
    """Build a .tar.gz from explicit TarInfo objects, bypassing filesystem limits.

    Constructing TarInfo directly is what lets us forge symlink/device members
    and traversal names portably — creating those on disk needs privileges on
    Windows and would make these tests skip exactly where they matter most.
    """
    with tarfile.open(str(dest), "w:gz") as tar:
        for info, payload in members:
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)


def _file_member(name: str, payload: bytes = b"{}") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.type = tarfile.REGTYPE
    return info, payload


def _dir_member(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


class SafeArchiveMemberNameTests(unittest.TestCase):
    """Guards the tar member names used to build extraction targets (Zip Slip)."""

    def test_rejects_parent_directory_traversal(self) -> None:
        for name in ("../escape", "profile/../../escape", "a/b/../../../etc/passwd"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Service.safe_archive_member_name(name)

    def test_rejects_absolute_paths(self) -> None:
        for name in ("/etc/passwd", "//server/share/x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Service.safe_archive_member_name(name)

    def test_rejects_backslash_traversal(self) -> None:
        # Backslashes are normalised to '/' first, so a Windows-style traversal
        # cannot slip past a '..'-parts check that only looked at POSIX parts.
        with self.assertRaises(ValueError):
            Service.safe_archive_member_name(r"..\..\evil")

    def test_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            Service.safe_archive_member_name("")

    def test_accepts_ordinary_nested_member(self) -> None:
        self.assertEqual(str(Service.safe_archive_member_name("profile/state.json")), "profile/state.json")


class ResolveContainedPathTests(unittest.TestCase):
    """Guards every filesystem target derived from caller-supplied input."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_relative_escape(self) -> None:
        with self.assertRaises(PermissionError):
            Service.resolve_contained_path(self.root, "../outside")

    def test_rejects_absolute_path_by_default(self) -> None:
        with self.assertRaises(PermissionError):
            Service.resolve_contained_path(self.root, str(self.root.parent / "outside"))

    def test_rejects_absolute_path_outside_root_even_when_allowed(self) -> None:
        with self.assertRaises(PermissionError):
            Service.resolve_contained_path(self.root, str(self.root.parent / "outside"), allow_absolute=True)

    def test_accepts_absolute_path_inside_root_when_allowed(self) -> None:
        target = self.root / "inside.json"
        self.assertEqual(Service.resolve_contained_path(self.root, str(target), allow_absolute=True), target)

    def test_accepts_nested_relative_path(self) -> None:
        self.assertEqual(Service.resolve_contained_path(self.root, "a/b/c.json"), self.root / "a" / "b" / "c.json")

    def test_root_itself_is_contained(self) -> None:
        self.assertEqual(Service.resolve_contained_path(self.root, "."), self.root)

    def test_sibling_prefix_directory_is_not_treated_as_inside(self) -> None:
        # "<root>-evil" shares a string prefix with "<root>" but is not inside it;
        # the guard must compare on a separator boundary, not raw startswith.
        sibling = self.root.parent / (self.root.name + "-evil")
        sibling.mkdir()
        with self.assertRaises(PermissionError):
            Service.resolve_contained_path(self.root, str(sibling), allow_absolute=True)


class NormalizeNameTests(unittest.TestCase):
    """Profile names become directory names, so the whitelist is load-bearing."""

    def test_rejects_traversal_and_separators(self) -> None:
        for name in ("../evil", "foo/bar", "foo\\bar", "..", "."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Service.normalize_name(name)

    def test_rejects_empty_or_whitespace(self) -> None:
        for name in ("", "   "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Service.normalize_name(name)

    def test_rejects_leading_punctuation(self) -> None:
        for name in (".hidden", "-dash", "_under"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Service.normalize_name(name)

    def test_rejects_overlong_name(self) -> None:
        with self.assertRaises(ValueError):
            Service.normalize_name("a" * 121)

    def test_accepts_valid_names(self) -> None:
        for name in ("my-profile", "profile.1", "a", "A_b-c.d", "a" * 120):
            with self.subTest(name=name):
                self.assertEqual(Service.normalize_name(name), name)

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(Service.normalize_name("  my-profile  "), "my-profile")


class ImportProfileSecurityTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end rejection behaviour of the archive import path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_root = Path(self._tmp.name).resolve()
        self.service = _service(self.auth_root)
        self.profile_root = self.auth_root / "profiles"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _archive(self, name: str) -> Path:
        return self.auth_root / name

    async def test_archive_path_traversal_cannot_reach_outside_auth_root(self) -> None:
        # The directory component is discarded and only the basename is looked up
        # inside auth_root, so a traversal resolves to a file that isn't there
        # rather than reading the attacker's target. Plant a real archive outside
        # the root to prove it is never opened.
        outside = self.auth_root.parent / "outside.tar.gz"
        _tar_with([_dir_member("stolen/"), _file_member("stolen/state.json", b'{"pwned": true}')], outside)
        try:
            with self.assertRaises(FileNotFoundError):
                await self.service.import_profile("../outside.tar.gz")
            self.assertFalse((self.profile_root / "stolen").exists())
        finally:
            outside.unlink(missing_ok=True)

    async def test_rejects_non_targz_archive_name(self) -> None:
        for name in ("profile.txt", "profile.zip", "profile.tar"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.service.import_profile(name)

    async def test_missing_archive_raises_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await self.service.import_profile("absent.tar.gz")

    async def test_rejects_symlink_member(self) -> None:
        # A symlink pointing outside the root is the classic tar escape: the
        # extractor writes through the link. The service must refuse outright.
        link = tarfile.TarInfo("profile/evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive = self._archive("payload.tar.gz")
        _tar_with([_dir_member("profile/"), _file_member("profile/state.json"), (link, None)], archive)

        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_hardlink_member(self) -> None:
        link = tarfile.TarInfo("profile/evil-hardlink")
        link.type = tarfile.LNKTYPE
        link.linkname = "profile/state.json"
        archive = self._archive("payload.tar.gz")
        _tar_with([_dir_member("profile/"), _file_member("profile/state.json"), (link, None)], archive)

        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_device_member(self) -> None:
        dev = tarfile.TarInfo("profile/evil-dev")
        dev.type = tarfile.CHRTYPE
        archive = self._archive("payload.tar.gz")
        _tar_with([_dir_member("profile/"), _file_member("profile/state.json"), (dev, None)], archive)

        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_traversal_member_and_writes_nothing_outside_root(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with(
            [_dir_member("profile/"), _file_member("profile/../../escaped.json", b'{"pwned": true}')],
            archive,
        )

        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

        # The security property, asserted directly rather than inferred.
        self.assertFalse((self.auth_root.parent / "escaped.json").exists())
        self.assertFalse((self.auth_root / "escaped.json").exists())

    async def test_rejects_empty_archive(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with([], archive)
        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_multiple_top_level_directories(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with(
            [
                _dir_member("one/"),
                _file_member("one/state.json"),
                _dir_member("two/"),
                _file_member("two/state.json"),
            ],
            archive,
        )
        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_bare_top_level_file(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with([_file_member("state.json")], archive)
        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_rejects_archive_whose_top_level_is_not_a_valid_profile_name(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with([_dir_member(".hidden/"), _file_member(".hidden/state.json")], archive)
        with self.assertRaises(ValueError):
            await self.service.import_profile("payload.tar.gz")

    async def test_imports_valid_archive(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with(
            [_dir_member("my-profile/"), _file_member("my-profile/state.json", b'{"session": "abc"}')],
            archive,
        )

        result = await self.service.import_profile("payload.tar.gz")

        self.assertEqual(result["profile_name"], "my-profile")
        self.assertTrue(result["imported"])
        extracted = self.profile_root / "my-profile" / "state.json"
        self.assertTrue(extracted.exists())
        self.assertEqual(json.loads(extracted.read_text(encoding="utf-8")), {"session": "abc"})

    async def test_existing_profile_requires_overwrite(self) -> None:
        archive = self._archive("payload.tar.gz")
        _tar_with([_dir_member("my-profile/"), _file_member("my-profile/state.json", b'{"v": 1}')], archive)
        await self.service.import_profile("payload.tar.gz")

        with self.assertRaises(FileExistsError):
            await self.service.import_profile("payload.tar.gz")

    async def test_overwrite_replaces_existing_profile(self) -> None:
        first = self._archive("first.tar.gz")
        _tar_with([_dir_member("my-profile/"), _file_member("my-profile/stale.json", b'{"old": true}')], first)
        await self.service.import_profile("first.tar.gz")

        second = self._archive("second.tar.gz")
        _tar_with([_dir_member("my-profile/"), _file_member("my-profile/state.json", b'{"v": 2}')], second)
        await self.service.import_profile("second.tar.gz", overwrite=True)

        profile_dir = self.profile_root / "my-profile"
        self.assertTrue((profile_dir / "state.json").exists())
        # Overwrite must replace the directory, not merge into it.
        self.assertFalse((profile_dir / "stale.json").exists())


class SafeAuthPathTests(unittest.TestCase):
    """`safe_auth_path` builds paths from request-supplied relative strings."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_root = Path(self._tmp.name).resolve()
        self.service = _service(self.auth_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_traversal(self) -> None:
        for relative in ("../escape.json", "a/../../escape.json"):
            with self.subTest(relative=relative), self.assertRaises(PermissionError):
                self.service.safe_auth_path(relative)

    def test_rejects_absolute_path_outside_root(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.safe_auth_path(str(self.auth_root.parent / "escape.json"))

    def test_accepts_nested_relative_path(self) -> None:
        self.assertEqual(self.service.safe_auth_path("nested/state.json"), self.auth_root / "nested" / "state.json")

    def test_must_exist_raises_for_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.service.safe_auth_path("nested/absent.json", must_exist=True)


if __name__ == "__main__":
    unittest.main()
