"""R5C-3: a provision cancelled while its worker thread creates the container and network releases them.

provision() awaited the worker thread before the try whose rollback releases a failed provision: cancelled
meanwhile, the await dropped the thread's result, the thread went on to create the browser container and the session
network, and nothing released them (or emptied the profile) until the next startup sweep.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from app import session_isolation
from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

SESSION = "session-cancelled"
# Every wait in these tests is bounded by this, so a regression fails instead of hanging.
WAIT_SECONDS = 5.0


def _provisioner(tmp_path: Path) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"))
    controller = FakeDockerContainer(
        container_id="controller-id",
        name="controller",
        attrs={
            "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
            "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
        },
    )
    client = FakeDockerClient(controller)
    provisioner = DockerBrowserNodeProvisioner(settings, client=client)
    provisioner._controller_container_id = controller.id
    return provisioner, client


class _HeldProvision:
    """The provisioner's own _provision_container_sync, held at its start until the test lets it go."""

    def __init__(self, provisioner: DockerBrowserNodeProvisioner, *, fails: bool = False) -> None:
        self.real = provisioner._provision_container_sync
        self.fails = fails
        self.entered = threading.Event()
        self.go = threading.Event()
        self.finished = threading.Event()
        provisioner._provision_container_sync = self

    def __call__(self, session_id: str):
        self.entered.set()
        self.go.wait(WAIT_SECONDS)
        try:
            if self.fails:
                raise RuntimeError("container start failed")
            return self.real(session_id)
        finally:
            self.finished.set()


async def _cancelled_while_creating(provisioner: DockerBrowserNodeProvisioner) -> asyncio.Task:
    task = asyncio.create_task(provisioner.provision(SESSION))
    assert await asyncio.to_thread(provisioner._provision_container_sync.entered.wait, WAIT_SECONDS)
    task.cancel()
    return task


async def _eventually(check) -> bool:
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_a_provision_cancelled_mid_create_releases_what_the_thread_made(tmp_path: Path) -> None:
    provisioner, client = _provisioner(tmp_path)
    held = _HeldProvision(provisioner)

    task = await _cancelled_while_creating(provisioner)
    await asyncio.sleep(0.05)  # the cancellation lands while the thread is still creating
    held.go.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_SECONDS)
    assert await asyncio.to_thread(held.finished.wait, WAIT_SECONDS)

    container = client.containers.browser_containers[provisioner._container_name(SESSION)]
    network = provisioner._session_network_name(container.name)
    profile = provisioner._local_runtime_root(SESSION)
    assert await _eventually(lambda: container.removed), "the container the thread created was left running"
    assert await _eventually(lambda: network not in client.networks.by_name), "its session network was left"
    assert await _eventually(lambda: not profile.exists()), "its profile and downloads were left"


async def test_the_cancellation_waits_for_the_thread_only_so_long_and_the_release_still_follows(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(session_isolation, "CANCELLED_PROVISION_WAIT_SECONDS", 0.05)
    provisioner, client = _provisioner(tmp_path)
    held = _HeldProvision(provisioner)

    task = await _cancelled_while_creating(provisioner)
    done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)

    assert task in done and task.cancelled()
    assert not held.finished.is_set()  # the cancellation went on while the thread was still creating
    held.go.set()
    assert await asyncio.to_thread(held.finished.wait, WAIT_SECONDS)
    container = client.containers.browser_containers[provisioner._container_name(SESSION)]
    profile = provisioner._local_runtime_root(SESSION)
    assert await _eventually(lambda: container.removed), "the container the thread created was left running"
    # Removed after the container: waited for, not read at once (Codex review: the release may still be running).
    assert await _eventually(lambda: not profile.exists()), "its profile and downloads were left"


def test_a_provision_whose_event_loop_ends_first_is_released_by_its_thread(tmp_path: Path, monkeypatch) -> None:
    # Codex review: the release ran as a task of the event loop, so a loop that ended before the thread finished (a
    # shutdown: asyncio.run cancels the tasks left, then waits for the executor's threads) dropped the thread's result
    # again, and the container and network stayed until the next startup sweep.
    monkeypatch.setattr(session_isolation, "CANCELLED_PROVISION_WAIT_SECONDS", 0.05)
    provisioner, client = _provisioner(tmp_path)
    held = _HeldProvision(provisioner)

    async def cancel_mid_create_then_end() -> None:
        task = await _cancelled_while_creating(provisioner)
        await asyncio.wait({task}, timeout=WAIT_SECONDS)
        assert task.done() and not held.finished.is_set()
        # The thread goes on once the loop has begun to end.
        threading.Timer(0.2, held.go.set).start()

    asyncio.run(cancel_mid_create_then_end())

    assert held.finished.is_set()  # asyncio.run waited for the thread
    container = client.containers.browser_containers[provisioner._container_name(SESSION)]
    assert container.removed, "the container the thread created was left running"
    assert provisioner._session_network_name(container.name) not in client.networks.by_name
    assert not provisioner._local_runtime_root(SESSION).exists()


def test_a_result_the_thread_handed_back_before_the_shutdown_is_released(tmp_path: Path) -> None:
    # Codex review of the fix above: the thread returned its result before any cancel, and the shutdown (asyncio.run
    # cancels every task left) cancelled the provision and the task awaiting the thread before that result reached
    # either. The release awaited the cancelled task and never saw the result, so the container and network stayed.
    provisioner, client = _provisioner(tmp_path)
    held = _HeldProvision(provisioner)
    handed_back = threading.Event()
    hand_back = provisioner._provision_unless_abandoned
    tasks: list[asyncio.Task] = []

    def watched(*args):
        try:
            return hand_back(*args)
        finally:
            handed_back.set()

    provisioner._provision_unless_abandoned = watched

    async def end_while_the_result_is_on_its_way() -> None:
        tasks.append(asyncio.create_task(provisioner.provision(SESSION)))
        assert await asyncio.to_thread(held.entered.wait, WAIT_SECONDS)
        held.go.set()
        # The loop is held until the thread has returned: its result has not reached the loop when asyncio.run
        # cancels the tasks left, as when a shutdown's cancels land first.
        assert handed_back.wait(WAIT_SECONDS)

    asyncio.run(end_while_the_result_is_on_its_way())

    assert tasks[0].cancelled()
    container = client.containers.browser_containers[provisioner._container_name(SESSION)]
    assert container.removed, "the container the thread handed back was left running"
    assert provisioner._session_network_name(container.name) not in client.networks.by_name
    assert not provisioner._local_runtime_root(SESSION).exists()


async def test_a_thread_that_fails_after_the_cancel_has_nothing_left_to_release(tmp_path: Path, caplog) -> None:
    provisioner, client = _provisioner(tmp_path)
    held = _HeldProvision(provisioner, fails=True)

    with caplog.at_level(logging.WARNING, logger="app.session_isolation"):
        task = await _cancelled_while_creating(provisioner)
        held.go.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        assert await _eventually(lambda: f"isolated session {SESSION}" in caplog.text)

    assert client.containers.browser_containers == {}
    assert "container start failed" in caplog.text
