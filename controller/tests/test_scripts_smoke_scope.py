"""The smoke scripts only touch what the smoke run created, and refuse to start otherwise.

smoke_isolated_session.sh and its tunnel twin removed every container and network
carrying auto-browser.managed=true and ran `compose down` with no project, so on a
machine that also runs the operator's own stack the smoke deleted their live session
containers. Their own controller (docker_ephemeral mode) also removes every managed
container on the daemon when it starts, and a second compose project from the same
checkout shares ./data. So the smokes now run in their own compose project, fail closed
in smoke_preflight before any side effect, and clean up by a container-name prefix
(scripts/smoke_lib.sh).

The dynamic tests run the real smoke_lib.sh and the real smoke scripts in bash against a
stand-in docker that records its calls. The static tests cover every smoke script.
Repo-layout checks: skipped where scripts/ is absent (the controller image) or bash is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_scripts_token_argv import BASH, SCRIPTS, _bash_path

SMOKE_SCRIPTS = sorted(SCRIPTS.glob("smoke_*.sh")) if SCRIPTS.is_dir() else []
# The scripts that start a compose stack: every smoke_*.sh except the library.
SMOKE_RUNNERS = [path for path in SMOKE_SCRIPTS if path.name != "smoke_lib.sh"]
LABEL = "auto-browser.managed=true"
MODE = "auto-browser.mode=docker_ephemeral"
SESSION = "auto-browser.session_id=abc"

# Rows in containers.txt / networks.txt are "name|label,label"; stack.txt rows are
# "working_dir|compose project|container name". A `fail` file makes every call exit 1.
FAKE_DOCKER = r"""#!/usr/bin/env bash
state="$FAKE_DOCKER_DIR"
echo "$*" >> "$state/calls.log"
if [[ -e "$state/fail" ]]; then
  echo "Cannot connect to the Docker daemon" >&2
  exit 1
fi
filters=()
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == "--filter" ]] && filters+=("${args[i + 1]}")
done
list() {
  local file="$1" name labels ok f
  while IFS='|' read -r name labels; do
    [[ -n "$name" ]] || continue
    ok=1
    for f in "${filters[@]}"; do
      case "$f" in
        name=*) [[ "$name" =~ ${f#name=} ]] || ok=0 ;;
        label=*) [[ ",$labels," == *",${f#label=},"* || ",$labels," == *",${f#label=}="* ]] || ok=0 ;;
      esac
    done
    ((ok)) && echo "$name"
  done < "$file"
  return 0
}
case "$1 $2" in
  "ps -a") list "$state/containers.txt" ;;
  "network ls") list "$state/networks.txt" ;;
  "ps --format")
    # `docker ps --format '{{dir label}}|{{project label}}|{{.Names}}'`: the rows are stored in that shape.
    cat "$state/stack.txt"
    ;;
esac
exit 0
"""


def _smoke_env(fake_bin: Path) -> dict[str, str]:
    env = {"PATH": os.pathsep.join([str(fake_bin), str(Path(BASH).parent), os.environ["PATH"]])}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


class _FakeDockerCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.temp = Path(tempdir.name)
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir()
        docker = self.fake_bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
        docker.chmod(0o755)
        self.state = self.temp / "state"
        self.state.mkdir()
        for name in ("containers.txt", "networks.txt", "stack.txt"):
            (self.state / name).write_text("", encoding="utf-8")
        self.root = self.temp / "repo"
        self.root.mkdir()

    def put(self, name: str, rows: list[str]) -> None:
        (self.state / name).write_text("".join(f"{row}\n" for row in rows), encoding="utf-8", newline="\n")

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def bash(self, body: str) -> subprocess.CompletedProcess[str]:
        script = (
            f'export FAKE_DOCKER_DIR="{_bash_path(self.state)}"; ROOT_DIR="{_bash_path(self.root)}"; '
            f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; source "{_bash_path(SCRIPTS / "smoke_lib.sh")}"; {body}'
        )
        return subprocess.run(
            [BASH, "-c", script], capture_output=True, text=True, timeout=60, env=_smoke_env(self.fake_bin)
        )

    def removals(self) -> list[str]:
        return [c for c in self.calls() if c.split()[:1] == ["rm"] or c.startswith("network rm") or " down" in c]


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class SmokePreflightTests(_FakeDockerCase):
    def preflight(self, mode: str = "isolated") -> subprocess.CompletedProcess[str]:
        return self.bash(f"smoke_setup; smoke_preflight {mode}; echo PREFLIGHT_OK")

    def test_a_clean_daemon_passes(self) -> None:
        result = self.preflight()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PREFLIGHT_OK", result.stdout)

    def test_a_daemon_that_cannot_be_listed_is_refused(self) -> None:
        (self.state / "fail").write_text("", encoding="utf-8")

        result = self.preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PREFLIGHT_OK", result.stdout)
        self.assertIn("cannot list", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_an_existing_managed_container_is_refused_and_named(self) -> None:
        self.put("containers.txt", [f"operator-session-1|{LABEL},{MODE}", "unrelated|other=1"])

        result = self.preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PREFLIGHT_OK", result.stdout)
        self.assertIn("operator-session-1", result.stderr)
        self.assertNotIn("unrelated", result.stderr)
        self.assertIn("docker rm -f operator-session-1", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_an_existing_managed_network_is_refused_and_named(self) -> None:
        self.put("networks.txt", [f"operator-net|{LABEL},{MODE},{SESSION}"])

        result = self.preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operator-net", result.stderr)
        self.assertIn("docker network rm operator-net", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_a_running_stack_of_this_checkout_in_another_project_is_refused(self) -> None:
        root = _bash_path(self.root)
        self.put("stack.txt", [f"{root}|operator-project|operator-controller-1", "/elsewhere|x|other-checkout-1"])

        for mode in ("isolated", "shared"):
            with self.subTest(mode=mode):
                result = self.preflight(mode)

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PREFLIGHT_OK", result.stdout)
                self.assertIn("operator-controller-1", result.stderr)
                self.assertIn("operator-project", result.stderr)
                self.assertNotIn("other-checkout-1", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_a_stack_whose_directory_label_is_spelled_the_windows_way_is_still_found(self) -> None:
        # Compose on Windows records the project directory with backslashes (C:\Users\me\repo), Git Bash says
        # /c/Users/me/repo and pwd -W says C:/Users/me/repo: one directory, three spellings.
        root = _bash_path(self.root)
        drive = re.sub(r"^/([a-z])/", lambda m: m.group(1).upper() + ":/", root)
        spellings = {
            "backslashes": drive.replace("/", "\\"),
            "forward slashes": drive,
            "lowercase drive": drive[:1].lower() + drive[1:],
            "trailing slash": root + "/",
            "backslash trailing": drive.replace("/", "\\") + "\\",
        }
        for label, spelling in spellings.items():
            for mode in ("isolated", "shared"):
                with self.subTest(spelling=label, mode=mode):
                    self.put("stack.txt", [f"{spelling}|operator-project|operator-controller-1"])

                    result = self.preflight(mode)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("PREFLIGHT_OK", result.stdout)
                    self.assertIn("operator-controller-1", result.stderr)

    def test_a_stack_of_another_checkout_is_not_confused_with_this_one(self) -> None:
        root = _bash_path(self.root)
        self.put("stack.txt", [f"{root}-other|x|prefix-lookalike-1", f"{root}/sub|x|subdir-1", "|x|no-compose-dir"])

        self.assertEqual(self.preflight().returncode, 0)

    def test_running_containers_of_the_smokes_own_project_are_refused_with_the_command_that_clears_them(self) -> None:
        # Either a smoke is running (two at once would recreate each other's services and tear each other down),
        # or a killed run left the stack up.
        self.put("stack.txt", ["/elsewhere|auto-browser-smoke|auto-browser-smoke-controller-1"])

        for mode in ("isolated", "shared"):
            with self.subTest(mode=mode):
                result = self.preflight(mode)

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PREFLIGHT_OK", result.stdout)
                self.assertIn("auto-browser-smoke-controller-1", result.stderr)
                self.assertIn("a smoke run is in progress, or a killed run left this behind", result.stderr)
                self.assertIn("docker compose -p auto-browser-smoke down --remove-orphans", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_check_managed_is_a_check_of_its_own_that_the_smokes_repeat_before_the_controller_starts(self) -> None:
        self.assertEqual(self.bash("smoke_setup; smoke_check_managed; echo CHECK_OK").returncode, 0)
        self.put("networks.txt", [f"operator-net|{LABEL},{MODE},{SESSION}"])

        result = self.bash("smoke_setup; smoke_check_managed; echo CHECK_OK")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("CHECK_OK", result.stdout)
        self.assertIn("docker network rm operator-net", result.stderr)

    def test_shared_mode_ignores_session_containers_but_not_a_dead_daemon(self) -> None:
        self.put("containers.txt", [f"operator-session-1|{LABEL},{MODE}"])
        self.assertEqual(self.preflight("shared").returncode, 0)

        (self.state / "fail").write_text("", encoding="utf-8")
        self.assertNotEqual(self.preflight("shared").returncode, 0)


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class SmokeSetupTests(_FakeDockerCase):
    def setup_output(self, env_text: str, *, ambient: str = "") -> subprocess.CompletedProcess[str]:
        (self.root / ".env").write_text(env_text, encoding="utf-8")
        body = (
            f"{ambient} smoke_setup; "
            'echo "project=$SMOKE_PROJECT compose=$COMPOSE_PROJECT_NAME prefix=$ISOLATED_BROWSER_CONTAINER_PREFIX"'
        )
        return self.bash(body)

    def test_the_default_project_is_stable_and_ignores_compose_project_name(self) -> None:
        result = self.setup_output(
            "COMPOSE_PROJECT_NAME=operator-env\n", ambient="export COMPOSE_PROJECT_NAME=operator;"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "project=auto-browser-smoke compose=auto-browser-smoke prefix=auto-browser-smoke-session", result.stdout
        )

    def test_smoke_compose_project_names_the_project_and_env_cannot_change_it(self) -> None:
        result = self.setup_output(
            "SMOKE_COMPOSE_PROJECT=from-env\nSMOKE_PROJECT=from-env\nISOLATED_BROWSER_CONTAINER_PREFIX=browser-session\n",
            ambient="export SMOKE_COMPOSE_PROJECT=ci-smoke;",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project=ci-smoke compose=ci-smoke prefix=ci-smoke-session", result.stdout)

    def test_the_smoke_controller_is_never_pointed_at_a_remote_docker_daemon(self) -> None:
        # The smoke's controller reaps every managed container on the daemon it is given; preflight only looks at
        # this script's own daemon.
        (self.root / ".env").write_text(
            "ISOLATED_DOCKER_HOST=tcp://remote:2375\nISOLATED_HOST_DATA_ROOT=/remote/data\n", encoding="utf-8"
        )
        for ambient in ("", "export ISOLATED_DOCKER_HOST=tcp://shell:2375;"):
            with self.subTest(ambient=ambient):
                result = self.bash(
                    f"{ambient} smoke_setup; "
                    "bash -c 'echo \"host=[${ISOLATED_DOCKER_HOST-unset}] root=[${ISOLATED_HOST_DATA_ROOT-unset}]\"'"
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("host=[] root=[]", result.stdout)

    def test_ports_set_in_the_environment_win_over_the_ones_in_env(self) -> None:
        # `API_PORT=8010 ./scripts/smoke_isolated_session.sh` is how the smoke runs beside another stack.
        (self.root / ".env").write_text("API_PORT=8000\nNOVNC_PORT=6080\nOTHER=1\n", encoding="utf-8")
        show = "bash -c 'echo \"api=[${API_PORT-unset}] novnc=[${NOVNC_PORT-unset}] other=[${OTHER-unset}]\"'"

        ambient = self.bash(f"export API_PORT=8010 NOVNC_PORT=6090; smoke_setup; {show}")
        one = self.bash(f"export API_PORT=8010; smoke_setup; {show}")
        none = self.bash(f"smoke_setup; {show}")

        self.assertIn("api=[8010] novnc=[6090] other=[1]", ambient.stdout)
        self.assertIn("api=[8010] novnc=[6080] other=[1]", one.stdout)
        self.assertIn("api=[8000] novnc=[6080] other=[1]", none.stdout)

    def test_a_project_that_is_this_checkouts_own_default_is_refused(self) -> None:
        # A plain `docker compose` here uses the folder's name as its project. A smoke in that project would
        # take over, and finally tear down, the operator's stack; so would one in a folder named auto-browser-smoke.
        own = self.root.name  # "repo", all lowercase letters
        result = self.bash(f"export SMOKE_COMPOSE_PROJECT={own}; smoke_setup; echo SETUP_OK")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SETUP_OK", result.stdout)
        self.assertIn(f"'{own}'", result.stderr)
        self.assertIn("SMOKE_COMPOSE_PROJECT", result.stderr)

        named_like_the_default = self.temp / "Auto-Browser-Smoke"
        named_like_the_default.mkdir()
        script = f'ROOT_DIR="{_bash_path(named_like_the_default)}"; smoke_setup; echo SETUP_OK'
        result = subprocess.run(
            [
                BASH,
                "-c",
                f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; source "{_bash_path(SCRIPTS / "smoke_lib.sh")}"; {script}',
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=_smoke_env(self.fake_bin),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("auto-browser-smoke", result.stderr)

    def test_a_project_name_compose_would_reject_is_refused(self) -> None:
        result = self.setup_output("", ambient="export SMOKE_COMPOSE_PROJECT='x;touch y';")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SMOKE_COMPOSE_PROJECT", result.stderr)


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class SmokeCleanupScopeTests(_FakeDockerCase):
    def cleanup(self) -> subprocess.CompletedProcess[str]:
        return self.bash(
            'smoke_setup; COMPOSE=(docker compose -p "${SMOKE_PROJECT}" -f docker-compose.yml); smoke_cleanup'
        )

    def test_cleanup_removes_only_the_prefixed_session_containers_and_networks(self) -> None:
        pre = "auto-browser-smoke-session"
        self.put(
            "containers.txt",
            [
                f"{pre}-aaa|{LABEL},{MODE}",
                f"operator-session-bbb|{LABEL},{MODE}",
                f"{pre}-nolabels|other=1",
                f"{pre}-nomode|{LABEL}",
                f"xx{pre}-embedded|{LABEL},{MODE}",
            ],
        )
        self.put(
            "networks.txt",
            [
                f"{pre}-aaa-net|{LABEL},{MODE},{SESSION}",
                f"operator-session-bbb-net|{LABEL},{MODE},{SESSION}",
                f"{pre}-foreign-net|{LABEL}",
                f"{pre}-aaa-bridge|{LABEL},{MODE},{SESSION}",
            ],
        )

        result = self.cleanup()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith("rm ")], [f"rm -f {pre}-aaa"])
        self.assertEqual([c for c in calls if c.startswith("network rm")], [f"network rm {pre}-aaa-net"])
        down = [c for c in calls if c.split()[:1] == ["compose"]]
        self.assertEqual(len(down), 1, calls)
        self.assertIn("compose -p auto-browser-smoke", down[0])
        self.assertLess(calls.index(f"rm -f {pre}-aaa"), calls.index(down[0]))
        self.assertLess(calls.index(down[0]), calls.index(f"network rm {pre}-aaa-net"))

    def test_a_failing_daemon_removes_nothing(self) -> None:
        self.put("containers.txt", [f"auto-browser-smoke-session-aaa|{LABEL},{MODE}"])
        (self.state / "fail").write_text("", encoding="utf-8")

        result = self.cleanup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith(("rm ", "network rm"))], [])


@unittest.skipUnless(SMOKE_RUNNERS and BASH, "scripts/ or bash is not available here")
class SmokeScriptsRefuseBeforeTouchingAnythingTests(_FakeDockerCase):
    """The real tunnel and reverse-SSH smokes, run from a scratch copy of scripts/, with a live stack present."""

    def run_script(self, name: str) -> subprocess.CompletedProcess[str]:
        shutil.copytree(SCRIPTS, self.root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        tunnels = self.root / "data" / "tunnels"
        (tunnels / "sessions").mkdir(parents=True)
        for keep in (tunnels / "reverse-ssh.json", tunnels / "sessions" / "live.json"):
            keep.write_text("operator", encoding="utf-8")
        script = _bash_path(self.root / "scripts" / name)
        return subprocess.run(
            [BASH, script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**_smoke_env(self.fake_bin), "FAKE_DOCKER_DIR": _bash_path(self.state)},
            cwd=self.temp,
        )

    def assert_untouched(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "data" / "tunnels" / "reverse-ssh.json").read_text(encoding="utf-8"), "operator")
        self.assertTrue((self.root / "data" / "tunnels" / "sessions" / "live.json").exists())
        self.assertFalse((self.root / "data" / "ssh").exists())
        self.assertEqual([c for c in self.calls() if c.startswith(("compose", "rm ", "network rm"))], [])

    def test_the_tunnel_smoke_refuses_beside_a_managed_container(self) -> None:
        self.put("containers.txt", [f"operator-session-1|{LABEL},{MODE}"])

        result = self.run_script("smoke_isolated_session_tunnel.sh")

        self.assert_untouched(result)
        self.assertIn("operator-session-1", result.stderr)

    def test_the_tunnel_smoke_refuses_beside_this_checkouts_stack(self) -> None:
        self.put("stack.txt", [f"{_bash_path(self.root)}|operator|operator-controller-1"])

        self.assert_untouched(self.run_script("smoke_isolated_session_tunnel.sh"))

    def test_the_reverse_ssh_smoke_refuses_beside_this_checkouts_stack(self) -> None:
        self.put("stack.txt", [f"{_bash_path(self.root)}|operator|operator-controller-1"])

        self.assert_untouched(self.run_script("smoke_reverse_ssh.sh"))

    def test_the_reverse_ssh_smoke_refuses_when_docker_is_down(self) -> None:
        (self.state / "fail").write_text("", encoding="utf-8")

        self.assert_untouched(self.run_script("smoke_reverse_ssh.sh"))


@unittest.skipUnless(SMOKE_RUNNERS, "scripts/ is not available here")
class SmokeScriptsAreScopedTests(unittest.TestCase):
    def test_every_smoke_runs_the_preflight_before_any_side_effect(self) -> None:
        for script in SMOKE_RUNNERS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                preflight = text.find("\nsmoke_preflight ")
                self.assertGreater(preflight, 0, "no smoke_preflight call")
                for effect in ("\ntrap cleanup EXIT", "\nrm -f", "\nmkdir ", '\n"${COMPOSE[@]}"'):
                    position = text.find(effect)
                    self.assertTrue(position == -1 or preflight < position, f"{effect.strip()} comes first")
                self.assertEqual(text.count("trap cleanup EXIT"), 1)

    def test_the_isolated_smokes_look_again_after_the_build_and_before_their_controller_starts(self) -> None:
        # The controller reaps every managed container when it starts, and the image build before it takes minutes.
        for name in ("smoke_isolated_session.sh", "smoke_isolated_session_tunnel.sh"):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                build = text.index('"${COMPOSE[@]}" build browser-node controller\n')
                recheck = text.index("\nsmoke_check_managed\n")
                start = re.search(r'^"\$\{COMPOSE\[@\]\}" up -d [^\n]*\bcontroller\b', text, flags=re.M)
                self.assertIsNotNone(start, "no command starts the controller")
                self.assertLess(build, recheck)
                self.assertLess(recheck, start.start())
                self.assertNotIn("--build", start.group(0), "the build must come before the check, not inside up")

    def test_every_smoke_cleans_up_through_smoke_cleanup_only(self) -> None:
        for script in SMOKE_RUNNERS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                body = re.search(r"^cleanup\(\) \{\n(.*?)^\}", text, flags=re.M | re.S)
                self.assertIsNotNone(body, "no cleanup()")
                self.assertEqual(body.group(1).strip(), "smoke_cleanup")

    def test_every_smoke_names_its_own_project_and_ignores_the_ambient_one(self) -> None:
        for script in SMOKE_RUNNERS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertIn('source "${ROOT_DIR}/scripts/smoke_lib.sh"', text)
                self.assertLess(text.index("\nsmoke_setup\n"), text.index("\nCOMPOSE=("))
                composes = re.findall(r"^COMPOSE=\((.*)\)$", text, flags=re.M)
                self.assertTrue(composes, "no COMPOSE array")
                for compose in composes:
                    self.assertIn('-p "${SMOKE_PROJECT}"', compose)
                self.assertNotIn("COMPOSE_PROJECT_NAME:-", text)
                self.assertNotIn("load_repo_env", text, "smoke_setup loads .env")

    def test_the_smoke_helpers_live_only_in_smoke_lib(self) -> None:
        load_env = (SCRIPTS / "load_env.sh").read_text(encoding="utf-8")
        for name in ("smoke_", "SMOKE_MANAGED", "auto-browser.managed"):
            self.assertNotIn(name, load_env)
        for other in sorted(SCRIPTS.glob("*.sh")):
            if other.name.startswith("smoke_"):
                continue
            with self.subTest(script=other.name):
                self.assertNotIn("smoke_lib.sh", other.read_text(encoding="utf-8"))


CI_WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "ci.yml"


@unittest.skipUnless(CI_WORKFLOW.is_file(), ".github/workflows/ci.yml is not available here")
class CiSmokeStepTests(unittest.TestCase):
    def test_ci_runs_the_smoke_in_its_own_project_after_stopping_the_doctor_stack(self) -> None:
        text = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertFalse("COMPOSE_PROJECT_NAME" in text, "ci.yml pins a compose project name")
        down = text.index("run: docker compose down --remove-orphans")
        smoke = text.index("./scripts/smoke_isolated_session.sh")
        doctor = text.index("./scripts/doctor.sh")
        self.assertLess(doctor, down)
        self.assertLess(down, smoke)


REPO = SCRIPTS.parent
REMOVAL = re.compile(r"\bdocker\s+(?:(?:container|network|system)\s+)?(?:rm|prune)\b|\bxargs\b[^\n|]*\brm\b")
MANAGED = re.compile(r"auto-browser\.managed|SMOKE_MANAGED_LABEL|managed=true")
SMOKE_CLEANUP = re.compile(r"^smoke_cleanup\(\) \{\n.*?^\}\n", flags=re.M | re.S)


def removes_by_the_managed_label(text: str, *, allow_smoke_cleanup: bool = False) -> bool:
    """Whether a script, Makefile or workflow both names the managed label and removes containers or networks.

    Comments and echo lines (a message that tells the operator which docker rm to run) do not count. In
    scripts/smoke_lib.sh the body of smoke_cleanup, which removes by name prefix as well as by label, is the one
    place that may do both.
    """
    if allow_smoke_cleanup:
        text = SMOKE_CLEANUP.sub("", text)
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#") and not re.search(r"\becho\b", line)
    )
    return bool(MANAGED.search(code) and REMOVAL.search(code))


class ManagedLabelRemovalGuardTests(unittest.TestCase):
    def test_the_detector_sees_the_ways_a_script_could_remove_by_the_label(self) -> None:
        pipeline = "docker ps -aq --filter label=auto-browser.managed=true | xargs docker rm -f\n"
        loop = (
            'F=label=auto-browser.managed=true\nfor id in $(docker ps -aq --filter "$F"); do docker rm -f "$id"; done\n'
        )
        network = "docker network ls -q --filter label=auto-browser.managed=true | xargs -r docker network rm\n"
        prune = "docker container prune --filter label=auto-browser.managed=true -f\n"
        bare_xargs = "docker ps -aq --filter label=auto-browser.managed=true | xargs rm\n"
        for text in (pipeline, loop, network, prune, bare_xargs):
            with self.subTest(text=text):
                self.assertTrue(removes_by_the_managed_label(text))
        self.assertFalse(removes_by_the_managed_label("docker compose down --remove-orphans\ndocker rm -f one\n"))
        self.assertFalse(removes_by_the_managed_label("# docker rm -f x  auto-browser.managed\n"))

    def test_the_allowance_covers_only_the_body_of_smoke_cleanup(self) -> None:
        inside = 'smoke_cleanup() {\n  docker rm -f "$n"\n  docker ps --filter label=auto-browser.managed=true\n}\n'
        self.assertFalse(removes_by_the_managed_label(inside, allow_smoke_cleanup=True))
        self.assertTrue(removes_by_the_managed_label(inside))
        outside = (
            inside + 'other() {\n  docker ps -aq --filter label=auto-browser.managed=true\n  docker rm -f "$n"\n}\n'
        )
        self.assertTrue(removes_by_the_managed_label(outside, allow_smoke_cleanup=True))

    @unittest.skipUnless(SMOKE_RUNNERS, "scripts/ is not available here")
    def test_nothing_in_the_repo_removes_by_the_managed_label_outside_smoke_cleanup(self) -> None:
        files = [*sorted(SCRIPTS.glob("*.sh")), *sorted((REPO / ".github" / "workflows").glob("*.yml"))]
        if (REPO / "Makefile").is_file():
            files.append(REPO / "Makefile")
        self.assertIn(SCRIPTS / "smoke_lib.sh", files)
        for path in files:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertFalse(
                    removes_by_the_managed_label(text, allow_smoke_cleanup=path.name == "smoke_lib.sh"),
                    f"{path.name} removes containers or networks by auto-browser.managed",
                )


if __name__ == "__main__":
    unittest.main()
