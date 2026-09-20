"""Offline structural checks for the enrolled tenant compose template."""

from pathlib import Path


def main() -> None:
    compose = Path(__file__).with_name("compose.yml").read_text(encoding="utf-8")
    assert "ports:" not in compose
    assert "approval-broker:" in compose
    assert "TENANT_BROKER_ALIAS" in compose
    assert "external: true" in compose
    assert "TENANT_DATA_VOLUME" in compose
    print("Offline enrolled-tenant compose invariants passed.")


if __name__ == "__main__":
    main()
