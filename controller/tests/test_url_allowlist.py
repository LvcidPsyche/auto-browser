from app.host_policy import host_is_allowed


def test_user_hostname_is_exact_and_does_not_span_subdomains() -> None:
    assert host_is_allowed("example.com", ["example.com"])
    assert not host_is_allowed("shop.example.com", ["example.com"])


def test_public_suffix_entry_does_not_grant_registrable_domains() -> None:
    assert host_is_allowed("com", ["com"])
    assert not host_is_allowed("example.com", ["com"])
