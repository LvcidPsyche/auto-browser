from __future__ import annotations

import pytest

from tenant_stacks.policy import MAX_ALLOWED_HOSTS, normalize_hostname, normalize_hostnames


def test_normalizes_idna_lowercase_and_duplicates() -> None:
    assert normalize_hostname(" B\u00dcCHER.example ") == "xn--bcher-kva.example"
    assert normalize_hostnames("Example.com,example.com,B\u00dcCHER.example") == (
        "example.com",
        "xn--bcher-kva.example",
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com", "example.com/path", "example.com:443", "*.example.com",
        "127.0.0.1", "[::1]", "１２７．０．０．１", "127.1", "0x7f000001",
    ],
)
def test_rejects_non_dns_hostnames(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_hostname(value)


def test_bounds_the_allow_list() -> None:
    values = [f"host{number}.example" for number in range(MAX_ALLOWED_HOSTS + 1)]
    with pytest.raises(ValueError, match="at most"):
        normalize_hostnames(values)


def test_empty_policy_is_explicitly_supported_for_deny_all() -> None:
    assert normalize_hostnames([], allow_empty=True) == ()
