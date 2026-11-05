"""A persona file that exists but cannot be read is never overwritten.

`_read_object` used to answer {} for a file it could not read or parse, so the next
save wrote one persona over it and erased every other one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from app.proxy_personas import ProxyPersonaFileError, ProxyPersonaStore, warn_about_ownerless_personas

CORRUPT = '{"a": {"server": "http://a:1"'


def test_a_missing_file_reads_as_empty(tmp_path: Path):
    store = ProxyPersonaStore(tmp_path / "personas.json")
    assert store.list_personas() == []


@pytest.mark.parametrize("content", [CORRUPT, "[1, 2]"])
def test_save_refuses_and_leaves_an_unusable_file_untouched(tmp_path: Path, content: str):
    path = tmp_path / "personas.json"
    path.write_text(content, encoding="utf-8")
    store = ProxyPersonaStore(path)

    with pytest.raises(ProxyPersonaFileError) as caught:
        store.set_persona("z", server="http://z:1")
    assert str(path) not in str(caught.value)
    assert "fix or move" in str(caught.value)
    with pytest.raises(ProxyPersonaFileError):
        store.delete_persona("a")

    assert path.read_text(encoding="utf-8") == content


def test_reads_report_the_error_instead_of_an_empty_set(tmp_path: Path):
    path = tmp_path / "personas.json"
    path.write_text(CORRUPT, encoding="utf-8")
    store = ProxyPersonaStore(path)

    with pytest.raises(ProxyPersonaFileError):
        store.list_personas()
    with pytest.raises(ProxyPersonaFileError):
        store.resolve_proxy("a")


def test_a_transient_read_failure_does_not_wipe_the_file(tmp_path: Path):
    path = tmp_path / "personas.json"
    store = ProxyPersonaStore(path)
    for name in ("a", "b"):
        store.set_persona(name, server=f"http://{name}:1")
    before = path.read_text(encoding="utf-8")

    with patch.object(Path, "read_text", side_effect=PermissionError("sharing violation")):
        with pytest.raises(ProxyPersonaFileError):
            store.set_persona("c", server="http://c:1")

    assert path.read_text(encoding="utf-8") == before


def test_the_startup_warning_reports_an_unusable_file_and_does_not_crash(tmp_path: Path, caplog):
    path = tmp_path / "personas.json"
    path.write_text(CORRUPT, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="app.proxy_personas"):
        warn_about_ownerless_personas(ProxyPersonaStore(path), named_tokens=True)

    assert any(str(path) in record.getMessage() for record in caplog.records)
