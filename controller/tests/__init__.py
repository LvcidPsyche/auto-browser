"""Test-suite defaults that must hold wherever the suite runs.

The controller image COPYs `app/` and `tests/` and nothing else — not
`conftest.py` — so anything the suite needs in the Docker `controller-tests` job
has to live inside this package. Putting it in conftest.py instead produced 147
failures in a container-shaped run while passing on the host, which is the same
shape as the August-2026 finding that the Docker job silently ran 566 of 637
tests.
"""

import atexit
import os
import shutil
import tempfile

from app.config import Settings


def _settings_variable_names() -> set[str]:
    """Every environment variable name `Settings` reads, upper-cased.

    That is each field's `alias` and every string in its `validation_alias`
    (an `AliasChoices` such as APP_ENV / ENVIRONMENT); a field with neither is
    read by its own name. `Settings` matches names case-insensitively.
    """
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        declared = [field.alias, *getattr(field.validation_alias, "choices", [field.validation_alias])]
        strings = [name for name in declared if isinstance(name, str)]
        names.update(name.upper() for name in (strings or [field_name]))
    return names


# The suite's results must not depend on the developer's setup. `Settings` reads
# exported variables and then a `.env` in the working directory; `make test`
# feeds the container's environment from `.env` (compose `env_file`), and a host
# run from `controller/` reads the file itself. Either way a developer's tokens,
# allowed hosts or REQUIRE_OPERATOR_ID changed what tests saw. Clear every name
# `Settings` reads and stop it reading `.env`; the defaults below then apply, and
# tests that need a value pass it to `Settings(...)` or set it themselves.
_SETTINGS_VARIABLES = _settings_variable_names()
for _variable in [name for name in os.environ if name.upper() in _SETTINGS_VARIABLES]:
    del os.environ[_variable]
Settings.model_config = {**Settings.model_config, "env_file": None}

# Every data root defaults to /data/..., and tests that build Settings without
# overriding a root wrote there: on a host, the real /data (C:\data on Windows);
# under `make test`, which mounts ./data, the developer's own audit log and
# witness chains — test receipts appended to real chains, signed with the
# deployment's real witness key. Every root now points at a throwaway
# directory, removed when the run ends. Assigned, not defaulted: compose sets
# ARTIFACT_ROOT and nine other roots to /data/... in the container, so under
# `make test` and the CI controller-tests job a default never applied. Tests that
# need a particular root set it themselves, after this runs.
_DATA_ROOT = tempfile.mkdtemp(prefix="auto-browser-tests-")
atexit.register(shutil.rmtree, _DATA_ROOT, ignore_errors=True)
for _name, _relative in {
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "AUTH_ROOT": "auth",
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "WITNESS_ROOT": "witness",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "HARNESS_ROOT": "harness",
    "MEMORY_ROOT": "memory",
    "SKILLS_STAGING_ROOT": "skills-staging",
    "MESH_IDENTITY_DIR": "mesh/identity",
    "MESH_PEERS_PATH": "mesh/peers.json",
    "CRON_STORE_PATH": "crons/crons.json",
    "SHARE_REVOCATION_STORE_PATH": "share/revocations.json",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "COMPLIANCE_MANIFEST_PATH": "compliance-manifest.json",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
    "ISOLATED_TUNNEL_INFO_ROOT": "tunnels/sessions",
    "API_TOKEN_FILE": "api-token",
    "TAKEOVER_TOKEN_FILE": "takeover-token",
    "CLI_HOME": "cli-home",
}.items():
    os.environ[_name] = os.path.join(_DATA_ROOT, _relative)

# API_BIND_SCOPE defaults to `exposed` so that an undeclared deployment fails
# closed (app/auth_policy.py). A TestClient run is loopback by construction and
# publishes nothing, so the suite declares that rather than inheriting a
# production-safety default it does not model. Tests that exercise the exposed
# path build their own Settings and must not rely on this.
os.environ.setdefault("API_BIND_SCOPE", "loopback")

# A loopback controller with no API_BEARER_TOKEN now generates a token in
# API_TOKEN_FILE and demands it (app/api_token.py). Most of the suite was written
# for the old default, a tokenless loopback API, and sends no Authorization
# header; the escape hatch keeps modelling that. Tests of the new default
# (test_api_token.py) and of tokenless mode itself set it explicitly.
os.environ.setdefault("API_ALLOW_TOKENLESS_LOOPBACK", "true")

# A tokenless loopback controller answers only to loopback Host names unless
# CONTROLLER_ALLOWED_HOSTS says otherwise (app/middleware/http.py) — the guard
# against DNS rebinding. TestClient addresses every request to `testserver`, so
# the suite lists it beside the loopback names; no shipped default does (a
# single-label name a host on the same network can answer for). Tests of the
# guard itself build their own Settings.
os.environ.setdefault("CONTROLLER_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver")

# The lifespan makes the controller process non-dumpable (app/process_hardening.py), and several tests run the real
# lifespan in this process: the flag would then stay off for the rest of the run. The suite keeps itself dumpable;
# test_process_hardening.py runs the real call in a child process.
from app import process_hardening as _process_hardening  # noqa: E402


def _keep_the_test_runner_dumpable() -> bool:
    return False


REAL_MAKE_NON_DUMPABLE = _process_hardening.make_non_dumpable
_process_hardening.make_non_dumpable = _keep_the_test_runner_dumpable
