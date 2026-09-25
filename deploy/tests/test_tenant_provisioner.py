from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tenants"))

from provisioner import (  # noqa: E402
    CONTROL_NETWORK,
    CommandResult,
    ConcurrencyLimitError,
    ProvisioningConfig,
    ProvisioningError,
    TenantEnrollment,
    TenantProvisioner,
    stack_key_for,
)

PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class RecordingRunner:
    def __init__(self, *, control_network_exists: bool = False, foreign_resources: set[str] | None = None):
        self.control_network_exists = control_network_exists
        self.commands: list[tuple[tuple[str, ...], bool]] = []
        self.labels: dict[tuple[str, str], dict[str, str]] = {}
        for resource in foreign_resources or set():
            self.labels[("volume", resource)] = {"auto-browser.stack-key": "foreign"}

    def run(self, argv: tuple[str, ...], *, check: bool = True) -> CommandResult:
        command = tuple(argv)
        self.commands.append((command, check))
        if command == ("docker", "network", "inspect", CONTROL_NETWORK):
            return CommandResult(0 if self.control_network_exists else 1)
        if len(command) == 6 and command[:3] == ("docker", command[1], "inspect"):
            labels = self.labels.get((command[1], command[-1]))
            return CommandResult(0, json.dumps(labels) if labels is not None else "") if labels is not None else CommandResult(1)
        if len(command) == 8 and command[:3] == ("docker", command[1], "create"):
            self.labels[(command[1], command[-1])] = {command[4].split("=", 1)[0]: command[4].split("=", 1)[1], command[6].split("=", 1)[0]: command[6].split("=", 1)[1]}
        if len(command) == 4 and command[:3] == ("docker", command[1], "rm"):
            self.labels.pop((command[1], command[-1]), None)
        return CommandResult(0)


def provisioner(tmp_path: Path, runner: RecordingRunner, max_running: int = 1) -> TenantProvisioner:
    return TenantProvisioner(
        ProvisioningConfig(
            state_root=tmp_path,
            allowed_hosts="example.com",
            max_running=max_running,
            portal_assertion_public_key=PUBLIC_KEY,
        ),
        runner,
    )


def test_provision_is_idempotent_and_uses_exact_private_docker_commands(tmp_path: Path) -> None:
    runner = RecordingRunner()
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("immutable-user", "immutable-tenant")
    key = stack_key_for(enrollment)
    descriptor = manager.provision(enrollment)

    home = tmp_path / key
    compose = str((Path(__file__).parents[1] / "tenants" / "compose.yml").resolve())
    assert descriptor.stack_key == key
    assert descriptor.user_id == "immutable-user"
    assert descriptor.tenant_id == "immutable-tenant"
    assert descriptor.broker_mcp_url == f"http://tenant-broker-{key}:18001/mcp"
    assert "immutable-user" not in descriptor.compose_project
    assert runner.commands == [
        (("docker", "network", "inspect", CONTROL_NETWORK), False),
        (("docker", "network", "create", "--internal", CONTROL_NETWORK), True),
        (("docker", "network", "inspect", "--format", "{{json .Labels}}", f"ab-net-{key}"), False),
        (("docker", "network", "create", "--label", f"auto-browser.stack-key={key}", "--label", "auto-browser.component=tenant-network", f"ab-net-{key}"), True),
        (("docker", "network", "inspect", "--format", "{{json .Labels}}", f"ab-net-{key}"), False),
        (("docker", "volume", "inspect", "--format", "{{json .Labels}}", f"ab-data-{key}"), False),
        (("docker", "volume", "create", "--label", f"auto-browser.stack-key={key}", "--label", "auto-browser.component=tenant-data", f"ab-data-{key}"), True),
        (("docker", "volume", "inspect", "--format", "{{json .Labels}}", f"ab-data-{key}"), False),
        (("docker", "volume", "inspect", "--format", "{{json .Labels}}", f"ab-broker-{key}"), False),
        (("docker", "volume", "create", "--label", f"auto-browser.stack-key={key}", "--label", "auto-browser.component=broker-state", f"ab-broker-{key}"), True),
        (("docker", "volume", "inspect", "--format", "{{json .Labels}}", f"ab-broker-{key}"), False),
        (
            (
                "docker", "compose", "--project-name", f"ab-{key}", "--env-file", str(home / ".env"),
                "-f", compose, "--profile", "tenant", "up", "-d", "--build",
            ),
            True,
        ),
    ]
    env = (home / ".env").read_text(encoding="utf-8")
    assert descriptor.portal_owner_token in env
    assert descriptor.gateway_agent_token in env
    assert f"BROKER_PORTAL_ASSERTION_PUBLIC_KEY={PUBLIC_KEY}" in env
    assert "BROKER_USER_ID=immutable-user" in env
    assert "BROKER_TENANT_ID=immutable-tenant" in env
    assert manager.provision(enrollment) == descriptor
    assert len(runner.commands) == 15
    assert not any(command[1] == "compose" for command, _ in runner.commands[12:])


def test_idle_then_resume_preserves_private_state_and_runs_only_compose(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    runner.commands.clear()

    manager.stop_for_idle(enrollment)
    assert (tmp_path / key / "descriptor.json").exists()
    manager.provision(enrollment)
    assert [command for command, _ in runner.commands if command[1] == "compose"] == [
        (
            "docker", "compose", "--project-name", f"ab-{key}", "--env-file", str(tmp_path / key / ".env"),
            "-f", str((Path(__file__).parents[1] / "tenants" / "compose.yml").resolve()), "--profile", "tenant", "stop",
        ),
        (
            "docker", "compose", "--project-name", f"ab-{key}", "--env-file", str(tmp_path / key / ".env"),
            "-f", str((Path(__file__).parents[1] / "tenants" / "compose.yml").resolve()), "--profile", "tenant", "up", "-d", "--build",
        ),
    ]


def test_deprovision_removes_stack_volumes_and_all_private_metadata(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    runner.commands.clear()

    manager.deprovision(enrollment)

    assert not (tmp_path / key).exists()
    assert [command for command, _ in runner.commands if len(command) == 4 and command[2] == "rm"] == [
        ("docker", "network", "rm", f"ab-net-{key}"),
        ("docker", "volume", "rm", f"ab-data-{key}"),
        ("docker", "volume", "rm", f"ab-broker-{key}"),
    ]


def test_refuses_reused_state_with_different_immutable_ownership(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    metadata_path = tmp_path / key / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["user_id"] = "different-user"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ProvisioningError, match="ownership"):
        manager.lookup(enrollment)


def test_running_bound_refuses_a_second_enrollment_before_docker(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner, max_running=1)
    manager.provision(TenantEnrollment("user-a", "tenant-a"))
    command_count = len(runner.commands)

    with pytest.raises(ConcurrencyLimitError):
        manager.provision(TenantEnrollment("user-b", "tenant-b"))
    assert len(runner.commands) == command_count


def test_rejects_non_ed25519_portal_public_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="32-byte Ed25519"):
        ProvisioningConfig(
            state_root=tmp_path,
            allowed_hosts="example.com",
            max_running=1,
            portal_assertion_public_key="AA",
        )


def test_reap_idle_stops_only_expired_stack_and_keeps_its_private_state(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = TenantProvisioner(
        ProvisioningConfig(
            state_root=tmp_path,
            allowed_hosts="example.com",
            max_running=2,
            portal_assertion_public_key=PUBLIC_KEY,
            idle_timeout_seconds=60,
        ),
        runner,
    )
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    manager.touch_activity(enrollment, now=100.0)
    runner.commands.clear()

    assert manager.reap_idle(now=159.0) == ()
    assert manager.reap_idle(now=160.0) == (key,)
    assert (tmp_path / key / "descriptor.json").exists()
    assert json.loads((tmp_path / key / "metadata.json").read_text(encoding="utf-8"))["status"] == "idle"
    assert [command[-1] for command, _ in runner.commands] == ["stop"]


def test_rotation_replaces_broker_credentials_and_recreates_only_the_broker(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    original = manager.provision(enrollment)
    runner.commands.clear()

    rotated = manager.rotate_credentials(enrollment)

    assert rotated.portal_owner_token != original.portal_owner_token
    assert rotated.gateway_agent_token != original.gateway_agent_token
    assert manager.lookup(enrollment) == rotated
    assert [command for command, _ in runner.commands] == [
        (
            "docker", "compose", "--project-name", f"ab-{key}", "--env-file", str(tmp_path / key / ".env"),
            "-f", str((Path(__file__).parents[1] / "tenants" / "compose.yml").resolve()), "--profile", "tenant",
            "up", "-d", "--no-deps", "--force-recreate", "approval-broker",
        )
    ]


def test_refuses_foreign_volume_before_compose_and_never_adopts_it(tmp_path: Path) -> None:
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    runner = RecordingRunner(control_network_exists=True, foreign_resources={f"ab-data-{key}"})
    manager = provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="ownership labels"):
        manager.provision(enrollment)

    assert [command for command, _ in runner.commands][-1] == (
        "docker", "volume", "inspect", "--format", "{{json .Labels}}", f"ab-data-{key}"
    )
    assert not any(command[:3] == ("docker", "compose", "--project-name") for command, _ in runner.commands)


def test_creating_state_retries_only_after_verifying_owned_resources(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    descriptor = manager._new_descriptor(enrollment, key)
    manager._create_private_state(tmp_path / key, enrollment, descriptor)

    assert manager.provision(enrollment) == descriptor
    assert json.loads((tmp_path / key / "metadata.json").read_text(encoding="utf-8"))["status"] == "running"
    assert any(command[-3:] == ("up", "-d", "--build") for command, _ in runner.commands)


class FailFirstControllerRecreateRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__(control_network_exists=True)
        self.controller_recreates = 0

    def run(self, argv: tuple[str, ...], *, check: bool = True) -> CommandResult:
        result = super().run(argv, check=check)
        if argv[-1:] == ("controller",):
            self.controller_recreates += 1
            if self.controller_recreates == 1:
                raise ProvisioningError("Docker lifecycle command failed")
        return result


class FailBothControllerRecreatesRunner(RecordingRunner):
    def run(self, argv: tuple[str, ...], *, check: bool = True) -> CommandResult:
        result = super().run(argv, check=check)
        if argv[-1:] == ("controller",) and "--force-recreate" in argv:
            raise ProvisioningError("Docker lifecycle command failed")
        return result


def test_allow_list_update_recreates_only_controller_and_persists_revision(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    runner.commands.clear()

    updated = manager.update_allowed_hosts(enrollment, "Example.com,B\u00dcCHER.example", expected_revision=1)

    assert updated.allowed_hosts == ("example.com", "xn--bcher-kva.example")
    assert updated.policy_revision == 2
    assert manager.lookup(enrollment) == updated
    assert "TENANT_ALLOWED_HOSTS=example.com,xn--bcher-kva.example\n" in (tmp_path / key / ".env").read_text()
    assert [command for command, _ in runner.commands] == [
        (
            "docker", "compose", "--project-name", f"ab-{key}", "--env-file", str(tmp_path / key / ".env"),
            "-f", str((Path(__file__).parents[1] / "tenants" / "compose.yml").resolve()), "--profile", "tenant",
            "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "60", "controller",
        )
    ]


def test_allow_list_update_rejects_a_stale_revision_before_changing_files(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    old_env = (tmp_path / key / ".env").read_text()
    old_descriptor = (tmp_path / key / "descriptor.json").read_text()
    runner.commands.clear()

    with pytest.raises(ProvisioningError, match="revision"):
        manager.update_allowed_hosts(enrollment, "other.example", expected_revision=0)

    assert (tmp_path / key / ".env").read_text() == old_env
    assert (tmp_path / key / "descriptor.json").read_text() == old_descriptor
    assert runner.commands == []


def test_allow_list_can_be_empty_and_serializes_a_non_resolving_deny_all_sentinel(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    runner.commands.clear()

    updated = manager.update_allowed_hosts(enrollment, [], expected_revision=1)

    assert updated.allowed_hosts == ()
    assert updated.policy_revision == 2
    assert manager.lookup(enrollment).allowed_hosts == ()
    assert "TENANT_ALLOWED_HOSTS=deny-all.invalid\n" in (tmp_path / key / ".env").read_text()
    assert runner.commands[-1][0][-1] == "controller"


def test_failed_allow_list_apply_restores_old_files_and_controller(tmp_path: Path) -> None:
    runner = FailFirstControllerRecreateRunner()
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    old_env = (tmp_path / key / ".env").read_text()
    old_descriptor = (tmp_path / key / "descriptor.json").read_text()
    runner.commands.clear()

    with pytest.raises(ProvisioningError, match="rolled back"):
        manager.update_allowed_hosts(enrollment, "replacement.example", expected_revision=1)

    assert (tmp_path / key / ".env").read_text() == old_env
    assert (tmp_path / key / "descriptor.json").read_text() == old_descriptor
    controller_commands = [command for command, _ in runner.commands]
    assert len(controller_commands) == 2
    assert all(command[-1] == "controller" for command in controller_commands)
    assert not any(f"ab-data-{key}" in command for command in controller_commands)


def test_failed_apply_and_rollback_stop_controller_to_prevent_divergence(tmp_path: Path) -> None:
    runner = FailBothControllerRecreatesRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    old_env = (tmp_path / key / ".env").read_text()
    old_descriptor = (tmp_path / key / "descriptor.json").read_text()
    runner.commands.clear()

    with pytest.raises(ProvisioningError, match="controller was stopped"):
        manager.update_allowed_hosts(enrollment, "replacement.example", expected_revision=1)

    assert (tmp_path / key / ".env").read_text() == old_env
    assert (tmp_path / key / "descriptor.json").read_text() == old_descriptor
    assert runner.commands[-1][0][-2:] == ("stop", "controller")


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and modes are production behavior")
def test_allow_list_update_preserves_private_file_mode_and_owner(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-a", "tenant-a")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    env_path = tmp_path / key / ".env"
    descriptor_path = tmp_path / key / "descriptor.json"
    env_path.chmod(0o640)
    descriptor_path.chmod(0o640)
    expected = [(path.stat().st_uid, path.stat().st_gid, stat.S_IMODE(path.stat().st_mode)) for path in (env_path, descriptor_path)]

    manager.update_allowed_hosts(enrollment, "changed.example", expected_revision=1)

    assert [(path.stat().st_uid, path.stat().st_gid, stat.S_IMODE(path.stat().st_mode)) for path in (env_path, descriptor_path)] == expected


def test_profile_control_token_is_generated_and_backfilled_for_older_stacks(tmp_path: Path) -> None:
    runner = RecordingRunner(control_network_exists=True)
    manager = provisioner(tmp_path, runner)
    enrollment = TenantEnrollment("user-token", "tenant-token")
    key = stack_key_for(enrollment)
    manager.provision(enrollment)
    env_path = tmp_path / key / ".env"
    values = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
    token = values["TENANT_PROFILE_CONTROL_TOKEN"]
    assert len(token) >= 40
    assert token not in (values["TENANT_CONTROLLER_TOKEN"], values["TENANT_SHARE_SECRET"])

    # A stack provisioned before the token existed: every compose call (even
    # the idle reaper's `stop`) would fail on the required variable, so it is
    # added once, and never rotated afterwards.
    legacy = "".join(f"{k}={v}\n" for k, v in values.items() if k != "TENANT_PROFILE_CONTROL_TOKEN")
    env_path.write_text(legacy, encoding="utf-8")
    manager.stop_for_idle(enrollment)
    backfilled = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
    assert backfilled["TENANT_PROFILE_CONTROL_TOKEN"]
    manager.provision(enrollment)
    again = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
    assert again["TENANT_PROFILE_CONTROL_TOKEN"] == backfilled["TENANT_PROFILE_CONTROL_TOKEN"]
