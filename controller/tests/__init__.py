"""Test-suite defaults that must hold wherever the suite runs.

The controller image COPYs `app/` and `tests/` and nothing else — not
`conftest.py` — so anything the suite needs in the Docker `controller-tests` job
has to live inside this package. Putting it in conftest.py instead produced 147
failures in a container-shaped run while passing on the host, which is the same
shape as the August-2026 finding that the Docker job silently ran 566 of 637
tests.
"""

import os

# API_BIND_SCOPE defaults to `exposed` so that an undeclared deployment fails
# closed (app/auth_policy.py). A TestClient run is loopback by construction and
# publishes nothing, so the suite declares that rather than inheriting a
# production-safety default it does not model. Tests that exercise the exposed
# path build their own Settings and must not rely on this.
os.environ.setdefault("API_BIND_SCOPE", "loopback")

# A tokenless loopback controller answers only to loopback Host names unless
# CONTROLLER_ALLOWED_HOSTS says otherwise (app/middleware/http.py) — the guard
# against DNS rebinding. TestClient addresses every request to `testserver`, so
# the suite declares it the way docker-compose.yml does. Tests of the guard
# itself build their own Settings.
os.environ.setdefault("CONTROLLER_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver")
