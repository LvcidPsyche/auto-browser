"""The suite writes nowhere but its own throwaway directory.

Data roots default to /data/..., and a test that built Settings without
overriding one wrote there: on a host, the real /data; under `make test`, which
mounts ./data, the developer's own audit log and witness chains. tests/__init__.py
points every root at a temp directory. This fails when a root is added to
Settings without one.
"""

from __future__ import annotations

from app.config import Settings

# Defaults the controller only reads: a key, a socket, a file another service writes.
READ_ONLY_DEFAULTS = {
    "browser_ws_endpoint_file",
    "isolated_tunnel_key_path",
    "isolated_tunnel_known_hosts_path",
    "openai_host_bridge_socket",
    "cli_home",
}


def test_no_writable_root_points_at_the_real_data_directory() -> None:
    settings = Settings(_env_file=None)
    leaking = sorted(
        name
        for name in Settings.model_fields
        if name not in READ_ONLY_DEFAULTS
        and isinstance(getattr(settings, name), str)
        and getattr(settings, name).startswith("/data")
    )
    assert leaking == [], f"add a default for these to tests/__init__.py: {leaking}"
