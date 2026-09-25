"""Bounded, idempotent provisioning for an enrolled tenant browser stack.

This trusted control-plane component receives immutable identifiers only after
authentication/enrollment. It never uses request or model-supplied values to
form a path, Docker name, command fragment, or network address.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterator, Protocol, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tenant_stacks.policy import normalize_hostnames

CONTROL_NETWORK = "auto-browser-tenant-control"
STACK_SCHEMA_VERSION = 1
DENY_ALL_HOST = "deny-all.invalid"


class ProvisioningError(RuntimeError):
    """The requested lifecycle operation was refused without guessing."""


class ConcurrencyLimitError(ProvisioningError):
    """Starting another stack would exceed the configured safe bound."""


@dataclass(frozen=True)
class TenantEnrollment:
    """Authenticated immutable identifiers supplied by trusted enrollment code."""

    user_id: str
    tenant_id: str

    def __post_init__(self) -> None:
        for value in (self.user_id, self.tenant_id):
            if not isinstance(value, str) or not value or len(value) > 256 or "\n" in value or "\r" in value:
                raise ValueError("Immutable identifiers must be non-empty strings no longer than 256 characters")


@dataclass(frozen=True)
class TenantStackDescriptor:
    """Private portal/gateway lookup record. Never return it to a browser client."""

    schema_version: int
    user_id: str
    tenant_id: str
    stack_key: str
    compose_project: str
    broker_control_alias: str
    broker_mcp_url: str
    portal_owner_token: str
    gateway_agent_token: str
    allowed_hosts: tuple[str, ...] = ()
    policy_revision: int = 0


@dataclass(frozen=True)
class ProvisioningConfig:
    state_root: Path
    allowed_hosts: str
    max_running: int
    portal_assertion_public_key: str
    idle_timeout_seconds: int = 900
    compose_file: Path | None = None
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        if self.max_running < 1:
            raise ValueError("max_running must be at least one")
        if self.idle_timeout_seconds < 1:
            raise ValueError("idle_timeout_seconds must be at least one")
        if self.allowed_hosts:
            normalize_hostnames(self.allowed_hosts)
        try:
            decoded_public_key = base64.urlsafe_b64decode(
                self.portal_assertion_public_key + "=" * (-len(self.portal_assertion_public_key) % 4)
            )
        except (ValueError, binascii.Error) as exc:
            raise ValueError("portal_assertion_public_key must be base64url") from exc
        if len(decoded_public_key) != 32:
            raise ValueError("portal_assertion_public_key must decode to a 32-byte Ed25519 public key")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""


class CommandRunner(Protocol):
    """Small seam for exact command tests; implementations must not log argv."""

    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        completed = subprocess.run(list(argv), check=False, capture_output=True, text=True)
        if check and completed.returncode:
            raise ProvisioningError("Docker lifecycle command failed")
        return CommandResult(completed.returncode, completed.stdout)


def stack_key_for(enrollment: TenantEnrollment) -> str:
    """Return a deterministic opaque key using immutable IDs only."""

    digest = hashlib.sha256()
    for value in (enrollment.user_id, enrollment.tenant_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:32]


# Secrets introduced after the first tenants were provisioned; see
# TenantProvisioner._ensure_generated_secrets.
_BACKFILLED_SECRETS = ("TENANT_PROFILE_CONTROL_TOKEN",)


def _secret() -> str:
    return secrets.token_urlsafe(36)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class TenantProvisioner:
    """Owns the lifecycle and private descriptor for isolated tenant stacks."""

    def __init__(self, config: ProvisioningConfig, runner: CommandRunner | None = None):
        self.config = config
        self.runner = runner or SubprocessRunner()
        self.root = self._validated_root(config.state_root)
        self.compose_file = (config.compose_file or Path(__file__).with_name("compose.yml")).resolve()
        if not self.compose_file.is_file() or self.compose_file.is_symlink():
            raise ValueError("compose_file must be a regular file")

    def provision(self, enrollment: TenantEnrollment) -> TenantStackDescriptor:
        key = stack_key_for(enrollment)
        home = self._home(key)
        if home.exists():
            descriptor, metadata = self._read_owned(home, enrollment, key)
            status = metadata.get("status")
            if status == "running":
                self._ensure_owned_resources(key)
                return descriptor
            if status not in {"idle", "creating"}:
                raise ProvisioningError("Existing stack has an unknown lifecycle state")
            self._require_capacity(excluding=key)
            self._ensure_owned_resources(key)
            self._compose(home, key, "up", "-d", "--build")
            self._write_metadata(home, enrollment, key, "running")
            return descriptor

        self._require_capacity()
        self._ensure_control_network()
        descriptor = self._new_descriptor(enrollment, key)
        self._create_private_state(home, enrollment, descriptor)
        self._ensure_owned_resources(key)
        self._compose(home, key, "up", "-d", "--build")
        self._write_metadata(home, enrollment, key, "running")
        return descriptor

    def stop_for_idle(self, enrollment: TenantEnrollment) -> None:
        key = stack_key_for(enrollment)
        home = self._home(key)
        _, metadata = self._read_owned(home, enrollment, key)
        if metadata.get("status") == "idle":
            return
        if metadata.get("status") != "running":
            raise ProvisioningError("Existing stack has an unknown lifecycle state")
        self._compose(home, key, "stop")
        self._write_metadata(home, enrollment, key, "idle")

    def deprovision(self, enrollment: TenantEnrollment) -> None:
        key = stack_key_for(enrollment)
        home = self._home(key)
        self._read_owned(home, enrollment, key)
        self._compose(home, key, "down", "--remove-orphans")
        self._remove_owned_resource("network", f"ab-net-{key}", key, "tenant-network")
        self._remove_owned_resource("volume", f"ab-data-{key}", key, "tenant-data")
        self._remove_owned_resource("volume", f"ab-broker-{key}", key, "broker-state")
        if home.is_symlink() or not home.is_dir():
            raise ProvisioningError("Refusing to remove an unsafe tenant state path")
        shutil.rmtree(home)

    def lookup(self, enrollment: TenantEnrollment) -> TenantStackDescriptor:
        """Read the exact server-side descriptor after authenticating enrollment."""

        key = stack_key_for(enrollment)
        descriptor, _ = self._read_owned(self._home(key), enrollment, key)
        return descriptor

    def update_allowed_hosts(
        self,
        enrollment: TenantEnrollment,
        allowed_hosts: str | Sequence[str],
        *,
        expected_revision: int | None = None,
    ) -> TenantStackDescriptor:
        """Atomically apply a canonical allow-list to one immutable tenant stack."""

        requested = normalize_hostnames(allowed_hosts, allow_empty=True)
        key = stack_key_for(enrollment)
        with self._tenant_lock(key):
            home = self._home(key)
            descriptor, metadata = self._read_owned(home, enrollment, key)
            if metadata.get("status") not in {"running", "idle"}:
                raise ProvisioningError("Existing stack has an unknown lifecycle state")
            if expected_revision is not None and expected_revision != descriptor.policy_revision:
                raise ProvisioningError("Allow-list policy revision does not match")
            previous = self._current_allowed_hosts(home, descriptor)
            is_legacy = not descriptor.allowed_hosts or descriptor.policy_revision == 0
            if requested == previous and not is_legacy:
                return descriptor

            replacement = replace(
                descriptor,
                allowed_hosts=requested,
                policy_revision=max(1, descriptor.policy_revision + (requested != previous)),
            )
            env_path = home / ".env"
            descriptor_path = home / "descriptor.json"
            old_env = self._read_private_text(env_path)
            old_descriptor = self._read_private_text(descriptor_path)
            values = self._parse_env(old_env)
            values["TENANT_ALLOWED_HOSTS"] = ",".join(requested) or DENY_ALL_HOST
            apply_started = False
            try:
                self._write_private(env_path, self._render_env(values))
                self._write_json(descriptor_path, asdict(replacement))
                if metadata.get("status") == "running" and requested != previous:
                    apply_started = True
                    self._recreate_controller(home, key)
            except Exception as exc:
                rollback_error: Exception | None = None
                try:
                    self._write_private(env_path, old_env)
                    self._write_private(descriptor_path, old_descriptor)
                    if apply_started:
                        self._recreate_controller(home, key)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                if rollback_error is not None:
                    try:
                        # Ambiguous controller state must fail closed. The
                        # persistent browser volume and auth profiles remain.
                        self._compose(home, key, "stop", "controller")
                    except Exception as stop_exc:
                        raise ProvisioningError(
                            "Allow-list update, rollback, and fail-closed stop all failed"
                        ) from stop_exc
                    raise ProvisioningError(
                        "Allow-list update rollback failed; controller was stopped"
                    ) from rollback_error
                raise ProvisioningError("Allow-list update was rolled back") from exc
            return replacement

    def touch_activity(self, enrollment: TenantEnrollment, *, now: float | None = None) -> None:
        """Record trusted portal/gateway activity for a running tenant only."""

        key = stack_key_for(enrollment)
        home = self._home(key)
        _, metadata = self._read_owned(home, enrollment, key)
        if metadata.get("status") != "running":
            raise ProvisioningError("Cannot record activity for a non-running tenant stack")
        self._write_metadata(home, enrollment, key, "running", last_activity=now)

    def reap_idle(self, *, now: float | None = None) -> tuple[str, ...]:
        """Stop expired running stacks while retaining all private volumes/state."""

        current = time.time() if now is None else now
        stopped: list[str] = []
        for child in self.root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            metadata = self._read_json(child / "metadata.json")
            if metadata.get("status") != "running":
                continue
            user_id, tenant_id, key, last_activity = (
                metadata.get("user_id"),
                metadata.get("tenant_id"),
                metadata.get("stack_key"),
                metadata.get("last_activity"),
            )
            if not isinstance(user_id, str) or not isinstance(tenant_id, str) or not isinstance(key, str):
                raise ProvisioningError("Refusing idle reaping for malformed tenant ownership")
            enrollment = TenantEnrollment(user_id, tenant_id)
            if key != stack_key_for(enrollment) or child != self._home(key):
                raise ProvisioningError("Refusing idle reaping for mismatched tenant ownership")
            if not isinstance(last_activity, (int, float)):
                raise ProvisioningError("Refusing idle reaping without a valid activity timestamp")
            if current - last_activity >= self.config.idle_timeout_seconds:
                self.stop_for_idle(enrollment)
                stopped.append(key)
        return tuple(stopped)

    def rotate_credentials(self, enrollment: TenantEnrollment) -> TenantStackDescriptor:
        """Replace only the portal/gateway broker credentials without exposing them."""

        key = stack_key_for(enrollment)
        home = self._home(key)
        descriptor, metadata = self._read_owned(home, enrollment, key)
        if metadata.get("status") not in {"running", "idle"}:
            raise ProvisioningError("Existing stack has an unknown lifecycle state")
        replacement = TenantStackDescriptor(
            schema_version=descriptor.schema_version,
            user_id=descriptor.user_id,
            tenant_id=descriptor.tenant_id,
            stack_key=descriptor.stack_key,
            compose_project=descriptor.compose_project,
            broker_control_alias=descriptor.broker_control_alias,
            broker_mcp_url=descriptor.broker_mcp_url,
            portal_owner_token=_secret(),
            gateway_agent_token=_secret(),
        )
        self._write_json(home / "descriptor.json", asdict(replacement))
        self._replace_broker_credentials(home, replacement)
        if metadata.get("status") == "running":
            self._compose(home, key, "up", "-d", "--no-deps", "--force-recreate", "approval-broker")
        return replacement

    def _validated_root(self, root: Path) -> Path:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("state_root must be an existing absolute non-symlink directory")
        resolved = root.resolve(strict=True)
        if os.name == "posix" and stat.S_IMODE(resolved.stat().st_mode) & 0o077:
            raise ValueError("state_root must be private (0700)")
        return resolved

    def _home(self, key: str) -> Path:
        home = self.root / key
        if home.parent != self.root:
            raise ProvisioningError("Unsafe tenant state path")
        return home

    def _new_descriptor(self, enrollment: TenantEnrollment, key: str) -> TenantStackDescriptor:
        alias = f"tenant-broker-{key}"
        return TenantStackDescriptor(
            schema_version=STACK_SCHEMA_VERSION,
            user_id=enrollment.user_id,
            tenant_id=enrollment.tenant_id,
            stack_key=key,
            compose_project=f"ab-{key}",
            broker_control_alias=alias,
            broker_mcp_url=f"http://{alias}:18001/mcp",
            portal_owner_token=_secret(),
            gateway_agent_token=_secret(),
            allowed_hosts=normalize_hostnames(self.config.allowed_hosts),
            policy_revision=1,
        )

    def _create_private_state(
        self, home: Path, enrollment: TenantEnrollment, descriptor: TenantStackDescriptor
    ) -> None:
        if home.exists() or home.is_symlink():
            raise ProvisioningError("Tenant state path already exists")
        home.mkdir(mode=0o700)
        self._write_json(home / "descriptor.json", asdict(descriptor))
        self._write_env(home, descriptor)
        self._write_metadata(home, enrollment, descriptor.stack_key, "creating")

    def _write_env(self, home: Path, descriptor: TenantStackDescriptor) -> None:
        values = {
            "COMPOSE_PROJECT_NAME": descriptor.compose_project,
            "TENANT_PRIVATE_NETWORK": f"ab-net-{descriptor.stack_key}",
            "TENANT_CONTROL_NETWORK": CONTROL_NETWORK,
            "TENANT_BROKER_ALIAS": descriptor.broker_control_alias,
            "TENANT_DATA_VOLUME": f"ab-data-{descriptor.stack_key}",
            "TENANT_BROKER_VOLUME": f"ab-broker-{descriptor.stack_key}",
            "TENANT_OPERATOR_ID": "tenant",
            "TENANT_CONTROLLER_TOKEN": _secret(),
            "TENANT_SHARE_SECRET": _secret(),
            "TENANT_FERNET_KEY": _fernet_key(),
            "TENANT_PROFILE_CONTROL_TOKEN": _secret(),
            "TENANT_ALLOWED_HOSTS": ",".join(descriptor.allowed_hosts),
            "BROKER_PORTAL_ASSERTION_PUBLIC_KEY": self.config.portal_assertion_public_key,
            "BROKER_USER_ID": descriptor.user_id,
            "BROKER_TENANT_ID": descriptor.tenant_id,
            "BROKER_OWNER_TOKEN": descriptor.portal_owner_token,
            "BROKER_AGENT_TOKENS": f"gateway:{descriptor.gateway_agent_token}",
        }
        self._write_private(home / ".env", "".join(f"{name}={value}\n" for name, value in values.items()))

    def _write_metadata(
        self,
        home: Path,
        enrollment: TenantEnrollment,
        key: str,
        status: str,
        *,
        last_activity: float | None = None,
    ) -> None:
        self._write_json(
            home / "metadata.json",
            {
                "schema_version": STACK_SCHEMA_VERSION,
                "user_id": enrollment.user_id,
                "tenant_id": enrollment.tenant_id,
                "stack_key": key,
                "status": status,
                "last_activity": time.time() if last_activity is None else last_activity,
            },
        )

    def _read_owned(
        self, home: Path, enrollment: TenantEnrollment, key: str
    ) -> tuple[TenantStackDescriptor, dict[str, object]]:
        if home.is_symlink() or not home.is_dir():
            raise ProvisioningError("Tenant stack does not exist or has an unsafe state path")
        metadata = self._read_json(home / "metadata.json")
        expected = {"user_id": enrollment.user_id, "tenant_id": enrollment.tenant_id, "stack_key": key}
        if any(metadata.get(field) != value for field, value in expected.items()):
            raise ProvisioningError("Refusing tenant state whose immutable ownership does not match")
        raw_descriptor = self._read_json(home / "descriptor.json")
        raw_hosts = raw_descriptor.get("allowed_hosts", ())
        if raw_hosts == () or raw_hosts == []:
            normalized_hosts: tuple[str, ...] = ()
        else:
            try:
                normalized_hosts = normalize_hostnames(raw_hosts)
            except (TypeError, ValueError) as exc:
                raise ProvisioningError("Tenant descriptor allow-list is malformed") from exc
        policy_revision = raw_descriptor.get("policy_revision", 0)
        if isinstance(policy_revision, bool) or not isinstance(policy_revision, int) or policy_revision < 0:
            raise ProvisioningError("Tenant descriptor policy revision is malformed")
        raw_descriptor["allowed_hosts"] = normalized_hosts
        raw_descriptor["policy_revision"] = policy_revision
        try:
            descriptor = TenantStackDescriptor(**raw_descriptor)
        except TypeError as exc:
            raise ProvisioningError("Tenant descriptor is malformed") from exc
        if any(getattr(descriptor, field) != value for field, value in expected.items()):
            raise ProvisioningError("Refusing descriptor whose immutable ownership does not match")
        return descriptor, metadata

    def _require_capacity(self, excluding: str | None = None) -> None:
        running = 0
        for child in self.root.iterdir():
            if child.name == excluding or child.is_symlink() or not child.is_dir():
                continue
            try:
                metadata = self._read_json(child / "metadata.json")
            except ProvisioningError:
                continue
            if metadata.get("status") == "running":
                running += 1
        if running >= self.config.max_running:
            raise ConcurrencyLimitError("Configured running-tenant limit reached")

    def _ensure_control_network(self) -> None:
        inspected = self.runner.run((self.config.docker_binary, "network", "inspect", CONTROL_NETWORK), check=False)
        if inspected.returncode:
            self.runner.run((self.config.docker_binary, "network", "create", "--internal", CONTROL_NETWORK))

    def _ensure_owned_resources(self, key: str) -> None:
        self._ensure_owned_resource("network", f"ab-net-{key}", key, "tenant-network")
        self._ensure_owned_resource("volume", f"ab-data-{key}", key, "tenant-data")
        self._ensure_owned_resource("volume", f"ab-broker-{key}", key, "broker-state")

    def _ensure_owned_resource(self, kind: str, name: str, key: str, component: str) -> None:
        labels = self._resource_labels(kind, name)
        expected = {"auto-browser.stack-key": key, "auto-browser.component": component}
        if labels is None:
            self.runner.run(
                (
                    self.config.docker_binary,
                    kind,
                    "create",
                    "--label",
                    f"auto-browser.stack-key={key}",
                    "--label",
                    f"auto-browser.component={component}",
                    name,
                )
            )
            labels = self._resource_labels(kind, name)
        if labels != expected:
            raise ProvisioningError("Refusing to reuse a Docker resource with different ownership labels")

    def _remove_owned_resource(self, kind: str, name: str, key: str, component: str) -> None:
        labels = self._resource_labels(kind, name)
        if labels is None:
            return
        expected = {"auto-browser.stack-key": key, "auto-browser.component": component}
        if labels != expected:
            raise ProvisioningError("Refusing to remove a Docker resource with different ownership labels")
        self.runner.run((self.config.docker_binary, kind, "rm", name))

    def _resource_labels(self, kind: str, name: str) -> dict[str, str] | None:
        result = self.runner.run(
            (self.config.docker_binary, kind, "inspect", "--format", "{{json .Labels}}", name), check=False
        )
        if result.returncode:
            return None
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProvisioningError("Docker resource labels could not be read") from exc
        if not isinstance(labels, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
            raise ProvisioningError("Docker resource labels are malformed")
        return labels

    def _ensure_generated_secrets(self, home: Path) -> None:
        """Backfill tenant secrets added after a stack was first provisioned.

        compose.yml requires every one of them (`${VAR:?...}`), so an older
        .env without them would make every compose call -- even `stop` from
        the idle reaper -- fail. Generated once, never rotated here.
        """
        path = home / ".env"
        values = self._parse_env(self._read_private_text(path))
        missing = [name for name in _BACKFILLED_SECRETS if not values.get(name)]
        if not missing:
            return
        for name in missing:
            values[name] = _secret()
        self._write_private(path, self._render_env(values))

    def _compose(self, home: Path, key: str, *operation: str) -> None:
        self._ensure_generated_secrets(home)
        self.runner.run(
            (
                self.config.docker_binary,
                "compose",
                "--project-name",
                f"ab-{key}",
                "--env-file",
                str(home / ".env"),
                "-f",
                str(self.compose_file),
                "--profile",
                "tenant",
                *operation,
            )
        )

    def _recreate_controller(self, home: Path, key: str) -> None:
        self._compose(
            home,
            key,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "60",
            "controller",
        )

    def _replace_broker_credentials(self, home: Path, descriptor: TenantStackDescriptor) -> None:
        path = home / ".env"
        values = self._parse_env(self._read_private_text(path))
        values["BROKER_OWNER_TOKEN"] = descriptor.portal_owner_token
        values["BROKER_AGENT_TOKENS"] = f"gateway:{descriptor.gateway_agent_token}"
        self._write_private(path, self._render_env(values))

    def _current_allowed_hosts(self, home: Path, descriptor: TenantStackDescriptor) -> tuple[str, ...]:
        if descriptor.allowed_hosts or descriptor.policy_revision > 0:
            return descriptor.allowed_hosts
        values = self._parse_env(self._read_private_text(home / ".env"))
        try:
            return normalize_hostnames(values["TENANT_ALLOWED_HOSTS"])
        except (KeyError, ValueError) as exc:
            raise ProvisioningError("Tenant environment allow-list is malformed") from exc

    @staticmethod
    def _parse_env(content: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in content.splitlines():
            name, separator, value = line.partition("=")
            if not separator or not name or name in values:
                raise ProvisioningError("Tenant environment is malformed")
            values[name] = value
        return values

    @staticmethod
    def _render_env(values: dict[str, str]) -> str:
        return "".join(f"{name}={value}\n" for name, value in values.items())

    @staticmethod
    def _read_private_text(path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            raise ProvisioningError("Tenant environment is missing or unsafe")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProvisioningError("Tenant environment is unreadable") from exc

    @contextmanager
    def _tenant_lock(self, key: str) -> Iterator[None]:
        locks = self.root / ".locks"
        if locks.is_symlink():
            raise ProvisioningError("Tenant lock directory is unsafe")
        locks.mkdir(mode=0o700, exist_ok=True)
        if not locks.is_dir() or locks.resolve(strict=True).parent != self.root:
            raise ProvisioningError("Tenant lock directory is unsafe")
        path = locks / f"{key}.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        if path.is_symlink() or not path.is_file():
            raise ProvisioningError("Tenant state file is missing or unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvisioningError("Tenant state file is unreadable") from exc
        if not isinstance(value, dict):
            raise ProvisioningError("Tenant state file is malformed")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, object]) -> None:
        TenantProvisioner._write_private(path, json.dumps(value, sort_keys=True) + "\n")

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ProvisioningError("Tenant state file is missing or unsafe")
        existing = path.stat() if path.exists() else None
        mode = stat.S_IMODE(existing.st_mode) if existing is not None else 0o600
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), mode)
                if existing is not None:
                    os.fchown(output.fileno(), existing.st_uid, existing.st_gid)
            output.write(content)
        os.replace(temporary, path)
