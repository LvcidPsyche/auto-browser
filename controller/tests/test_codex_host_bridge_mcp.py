"""The Codex host bridge starts no MCP server and keeps off each feature its look reads: each call first looks at what
`codex <the call's -c flags> features list` prints and refuses where it shows on a feature the look reads, then
asks Codex which servers it would start (`codex <the call's -c flags> mcp list --json`), then runs `codex exec` with
the project root pinned to its directory and each listed server turned off in one inline table: the look and the lists
with the call's settings, in the exec's folder, with its environment, to its deadline. Where the list shows a server
on, it runs once more with that table, and a server it still shows on refuses the call. Each command of a call carries
the call's settings as its -c values, in order, and no other but the server table, on the second list and the exec. A
look or a list that fails or prints no list refuses the call, one that hangs times it out as a hanging exec does, and
what it printed on stdout is in no line the bridge prints and, where it hung, not in the timed-out error's text, its
cause or its context. A refusal is a 409, which the controller's client does not retry.

Codex is a fake here: a Python script, run through the patched Popen, that records each call's argv, directory and
environment.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests._codex_bridge import BRIDGE_SCRIPT, load_bridge
from tests._codex_mcp_list import (
    BLANK_LINES_AND_CRLF,
    FIRST_WORD_NO_NAME,
    LOOKED_AT,
    NAMES,
    NEWER_FEATURES_LIST,
    NOT_UTF8_ROW,
    PINNED_FEATURES_LIST,
    SETTINGS,
    NoServers,
    config_values,
    features_list,
    is_features_list,
    is_mcp_list,
    mcp_list_json,
    turned_off,
)

_REAL_POPEN = subprocess.Popen
bridge = load_bridge("codex_host_bridge_mcp_under_test") if BRIDGE_SCRIPT.is_file() else None
PAYLOAD = {"prompt": "decide", "schema": {"type": "object"}}
# A marker for where a failed run's stderr is shown: one of its own, beside the stdout's sentinel-6c1e.
STDERR_MARKER = "stderr-9d2b"
# Codex's own failure on a malformed config.toml (codex-cli 0.114.0), with that marker.
CONFIG_ERROR = f"Error: failed to load configuration\nCaused by: config.toml:3:20: unclosed table {STDERR_MARKER}"
# A server the managed configuration keeps on (enabled = true), whatever the call's table says, and the marker for
# where its name is shown.
KEPT_ON = "managed-sentinel-6c1e"
# A value a list prints in a server's env, which the log must not show.
LISTED_VALUE = "value-7f3a"
# What a run that exits non-zero printed on stdout alone, with a marker the log must not show.
FAILED_STDOUT = json.dumps({"token": "sentinel-6c1e"})
# The look's refusal, written out: for shell_tool alone, for apps and shell_tool, and for apps, shell_tool and
# codex_hooks (in the order of the call's settings).
ONE_FEATURE_ON = (
    "the Codex configuration turns on the feature shell_tool, which a decision call needs off: turn it off where the "
    "configuration turns it on, such as in a selected profile, in the managed configuration, in a requirements file, "
    "or under an older name in a [features] table"
)
TWO_FEATURES_ON = (
    "the Codex configuration turns on the features apps, shell_tool, which a decision call needs off: turn them off "
    "where the configuration turns them on, such as in a selected profile, in the managed configuration, in a "
    "requirements file, or under an older name in a [features] table"
)
THREE_FEATURES_ON = (
    "the Codex configuration turns on the features apps, shell_tool, codex_hooks, which a decision call needs off: "
    "turn them off where the configuration turns them on, such as in a selected profile, in the managed "
    "configuration, in a requirements file, or under an older name in a [features] table"
)
# For every feature the look reads, in the order of the call's settings.
EVERY_FEATURE_ON = (
    "the Codex configuration turns on the features multi_agent, apps, shell_tool, codex_hooks, shell_snapshot, "
    "js_repl, code_mode, apply_patch_freeform, artifact, image_generation, request_permissions_tool, which a "
    "decision call needs off: turn them off where the configuration turns them on, such as in a selected profile, in "
    "the managed configuration, in a requirements file, or under an older name in a [features] table"
)
# The status of a call the bridge refuses before its exec, written out.
REFUSED = 409
LOOK_FAILED = (REFUSED, "codex features list failed; see the bridge log")
# The refusal of a call whose second list still shows a server on, written out.
SERVERS_STILL_ON = (
    "the Codex configuration keeps on MCP servers that a decision call turns off, so the Codex call was not made; "
    "see the bridge log"
)

FAKE_CODEX = r"""
import json, os, sys, time, tomllib
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "behaviour.json"), encoding="utf-8") as given:
    behaviour = json.load(given)
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as calls:
    calls.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
argv = sys.argv[1:]


def printed(kind):
    # Once what the run printed is out, before it waits: a line naming the run, as each call is noted.
    with open(os.path.join(here, "printed.txt"), "a", encoding="utf-8") as notes:
        notes.write(kind + "\n")


def answer(run, kind):
    time.sleep(run.get("sleep", 0))
    # As bytes: what a run prints is the same on every OS, with no newline written as CRLF; a lone surrogate (as
    # surrogateescape decodes a byte that is no UTF-8) is printed as that byte.
    sys.stdout.buffer.write(run.get("stdout", "").encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.write(run.get("stderr", "").encode("utf-8"))
    sys.stdout.flush()
    sys.stderr.flush()
    printed(kind)
    time.sleep(run.get("hang", 0))  # once what it printed is out
    sys.exit(run.get("exit", 0))


if argv[-2:] == ["features", "list"]:
    answer(behaviour["features"], "the look")
if argv[-3:] == ["mcp", "list", "--json"]:
    tables = [tomllib.loads(word) for word in argv if word.startswith("mcp_servers=")]
    if not tables:
        answer(behaviour["first"], "the first list")
    if behaviour["second"] is not None:
        answer(behaviour["second"], "the second list")
    # As Codex lists them: each server the table turns off is off, but those the managed configuration keeps on.
    off = {name for table in tables for name in table["mcp_servers"]} - set(behaviour["kept_on"])
    listed = json.loads(behaviour["first"]["stdout"])
    relisted = [dict(entry, enabled=entry["name"] not in off) for entry in listed]
    answer({"stdout": json.dumps(relisted)}, "the second list")
sys.stdout.write(behaviour.get("exec_stdout", ""))
sys.stdout.flush()
printed("the exec")
time.sleep(behaviour["exec_sleep"])
with open(argv[argv.index("--output-last-message") + 1], "w", encoding="utf-8") as out:
    out.write('{"action": "done"}')
"""


class _BridgeAgainstTheFake(unittest.TestCase):
    """One decision of the bridge's against FAKE_CODEX at a time (no test of its own)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake = self.dir / "fake_codex.py"
        self.fake.write_text(FAKE_CODEX, encoding="utf-8")
        self.programs: set[str] = set()

    def _popen(self, command, **kwargs):
        # The bridge's command with the fake in place of codex: what the bridge chose is what the fake records, and
        # the program it named is noted here.
        self.programs.add(command[0])
        return _REAL_POPEN([sys.executable, str(self.fake), *command[1:]], **kwargs)

    def _decide(
        self,
        stdout: str,
        *,
        exit_code: int = 0,
        stderr: str = "",
        sleep: float = 0,
        hang: float = 0,
        features: dict | None = None,
        second: dict | None = None,
        kept_on: tuple[str, ...] = (),
        exec_stdout: str = "",
        exec_sleep: float = 0,
        **service_options,
    ):
        """One decision against the fake, by a CodexBridgeService given ``service_options``: its first `mcp list
        --json` waits ``sleep``, prints ``stdout`` and ``stderr``, waits ``hang`` and exits with ``exit_code``, its
        second answers as ``second`` says, else as Codex does (each server the table turns off is off, but each of
        ``kept_on``), its `features list` as ``features`` says (else PINNED_FEATURES_LIST), and its exec prints
        ``exec_stdout`` and answers after ``exec_sleep``; (the reply or the BridgeError, the fake's calls, the bridge's
        log)."""
        behaviour = {
            "first": {"stdout": stdout, "exit": exit_code, "stderr": stderr, "sleep": sleep, "hang": hang},
            "second": second,
            "kept_on": list(kept_on),
            "features": features or {"stdout": PINNED_FEATURES_LIST},
            "exec_stdout": exec_stdout,
            "exec_sleep": exec_sleep,
        }
        (self.dir / "behaviour.json").write_text(json.dumps(behaviour), encoding="utf-8")
        (self.dir / "calls.jsonl").unlink(missing_ok=True)
        (self.dir / "printed.txt").unlink(missing_ok=True)
        self.service = bridge.CodexBridgeService(codex_path="codex", **service_options)
        captured = io.StringIO()
        with patch.object(bridge.subprocess, "Popen", side_effect=self._popen), redirect_stderr(captured):
            try:
                reply = self.service.handle_openai_decide(dict(PAYLOAD))
            except bridge.BridgeError as exc:
                reply = exc
        calls_file = self.dir / "calls.jsonl"
        lines = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
        return reply, [json.loads(line) for line in lines], captured.getvalue()

    def _printed(self) -> list[str]:
        """The runs of the last decision that printed what they were given: "the look", "the first list", "the second
        list" or "the exec"."""
        notes = self.dir / "printed.txt"
        return notes.read_text(encoding="utf-8").splitlines() if notes.exists() else []

    @staticmethod
    def _exec_argv(calls: list[dict]) -> list[str]:
        (argv,) = [call["argv"] for call in calls if "exec" in call["argv"]]
        return argv

    @staticmethod
    def _kinds(calls: list[dict]) -> list[str]:
        """Each run of the call, in order: the look, a list (with the table, or without), or the exec."""
        kinds = []
        for call in calls:
            if is_features_list(call["argv"]):
                kinds.append("look")
            elif is_mcp_list(call["argv"]):
                kinds.append("list with the table" if turned_off(call["argv"]) else "list")
            else:
                kinds.append("exec")
        return kinds

    def _only_stderr_and_the_stdouts_length_logged(self, name: str, run: dict, log: str) -> None:
        """Of what the failed ``run`` of ``name`` printed (its "stdout" and "stderr"), the bridge's log holds the first
        2000 characters of its stderr and how long its stdout was, and nothing of its stdout: the whole of the log is
        the one print written out here."""
        code = run.get("exit_code", run.get("exit", 0))
        ended = f"exited with {code}" if code else "exited with 0 and printed no list"
        length = len(run["stdout"].encode("utf-8", "surrogateescape"))
        errors = run.get("stderr", "").strip()[:2000] or "(nothing)"
        said = f"[codex-bridge] {name} {ended}: {length} bytes on stdout, not logged; stderr: {errors}\n"
        self.assertEqual(log, said)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeMcpServerTests(_BridgeAgainstTheFake):
    def test_each_server_codex_lists_is_turned_off_in_one_inline_table(self) -> None:
        reply, calls, _log = self._decide(mcp_list_json(NAMES))

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        values = config_values(self._exec_argv(calls))
        self.assertIn("project_root_markers=[]", values)
        self.assertNotIn("mcp_servers={}", values)
        tables = [value for value in values if value.startswith("mcp_servers")]
        self.assertEqual(len(tables), 1, tables)
        # Each name a TOML basic string that Codex reads back as the name it listed.
        self.assertEqual(tomllib.loads(tables[0]), {"mcp_servers": {name: {"enabled": False} for name in NAMES}})

    def test_the_look_and_the_lists_run_with_the_execs_flags(self) -> None:
        _reply, calls, _log = self._decide(mcp_list_json(["home"]))

        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table", "exec"], calls)
        look, listed, relisted, execution = (call["argv"] for call in calls)
        exec_values = config_values(execution)
        flags = [word for value in exec_values if not value.startswith("mcp_servers") for word in ("-c", value)]
        (table,) = [value for value in exec_values if value.startswith("mcp_servers")]
        self.assertEqual(look, flags + ["features", "list"])
        self.assertEqual(listed, flags + ["mcp", "list", "--json"])
        self.assertEqual(relisted, flags + ["-c", table, "mcp", "list", "--json"])  # the table the exec carries

    def test_the_commands_of_one_call_are_all_started_in_one_folder(self) -> None:
        # The call's own directory, the exec's --cd: Codex resolves a relative CODEX_HOME against the process's folder,
        # so each command reads the home the others read.
        _reply, calls, _log = self._decide(mcp_list_json(["home"]))

        self.assertEqual(len(calls), 4, calls)
        execution = self._exec_argv(calls)
        call_directory = Path(execution[execution.index("--cd") + 1]).resolve()
        self.assertEqual({Path(call["cwd"]).resolve() for call in calls}, {call_directory})

    def test_the_bridges_settings_are_the_settings_written_out_in_the_tests(self) -> None:
        self.assertEqual(bridge.CONFIG_OVERRIDES, SETTINGS)
        self.assertEqual(bridge.LOOKED_AT_FEATURES, LOOKED_AT)

    def test_each_codex_command_line_carries_the_settings_in_order_and_no_other_value_but_the_table(self) -> None:
        _reply, calls, _log = self._decide(mcp_list_json(["home"]))

        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table", "exec"], calls)
        for call, table_count in zip(calls, (0, 0, 1, 1), strict=True):
            values = config_values(call["argv"])
            tables = [value for value in values if value.startswith("mcp_servers=")]
            self.assertEqual(len(tables), table_count, call["argv"])
            self.assertEqual(values, [*SETTINGS, *tables], call["argv"])

    def test_the_execs_command_line_is_written_out_whole(self) -> None:
        # Word for word, so that a word outside its -c pairs (another flag, a long --config) is one too many.
        cases = {"a server": (["home"], None), "a server and a model": (["home"], "gpt-test"), "no server": ([], None)}
        for case, (names, model) in cases.items():
            with self.subTest(case=case):
                _reply, calls, _log = self._decide(mcp_list_json(names), default_model=model)

                execution = self._exec_argv(calls)
                folder = Path(execution[execution.index("--cd") + 1])
                self.assertEqual(
                    execution,
                    [
                        *(["--model", "gpt-test"] if model else []),
                        *(word for value in SETTINGS for word in ("-c", value)),
                        *(["-c", 'mcp_servers={"home"={enabled=false}}'] if names else []),
                        "exec",
                        "--skip-git-repo-check",
                        "-s",
                        "read-only",
                        "--cd",
                        str(folder),
                        "--ephemeral",
                        "--output-schema",
                        str(folder / "schema.json"),
                        "--output-last-message",
                        str(folder / "decision.json"),
                        "-",
                    ],
                )
        self.assertEqual(self.programs, {"codex"})  # the program of every command of those calls

    def test_no_server_listed_means_one_list_and_no_table(self) -> None:
        reply, calls, _log = self._decide("[]")

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        self.assertEqual(self._kinds(calls), ["look", "list", "exec"])
        values = config_values(self._exec_argv(calls))
        self.assertIn("project_root_markers=[]", values)
        self.assertEqual([value for value in values if value.startswith("mcp_servers")], [])

    def test_every_server_listed_off_means_one_list(self) -> None:
        # As Codex lists them where /etc/codex/requirements.toml turns every server off.
        reply, calls, _log = self._decide(mcp_list_json(["home", "mgd"], off=["home", "mgd"]))

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        self.assertEqual(self._kinds(calls), ["look", "list", "exec"])
        self.assertEqual(turned_off(self._exec_argv(calls)), {"home", "mgd"})

    def test_a_second_list_that_still_shows_a_server_on_refuses_the_call(self) -> None:
        reply, calls, log = self._decide(mcp_list_json(["home", KEPT_ON], value=LISTED_VALUE), kept_on=(KEPT_ON,))

        self.assertIsInstance(reply, bridge.BridgeError)
        self.assertEqual((reply.status_code, reply.message), (REFUSED, SERVERS_STILL_ON))
        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table"], "codex exec ran")
        self.assertNotIn("sentinel", reply.message)
        # Its name stays on the bridge side: of what a list prints, the one thing logged.
        self.assertIn(json.dumps([KEPT_ON]), log)
        self.assertNotIn('"home"', log)  # a server the table turned off is not named
        self.assertNotIn(LISTED_VALUE, log)

    def test_a_first_list_with_a_server_off_and_one_on_means_a_second_list_with_every_name(self) -> None:
        # home listed off, and a server the managed configuration keeps on listed on, at both lists.
        reply, calls, _log = self._decide(mcp_list_json(["home", KEPT_ON], off=["home"]), kept_on=(KEPT_ON,))

        self.assertIsInstance(reply, bridge.BridgeError)
        self.assertEqual((reply.status_code, reply.message), (REFUSED, SERVERS_STILL_ON))
        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table"], "codex exec ran")
        self.assertEqual(turned_off(calls[2]["argv"]), {"home", KEPT_ON})  # the second list's table: every name listed

    def test_a_first_list_with_a_server_off_and_one_on_gives_the_exec_a_table_with_every_name(self) -> None:
        # home listed off and other on at the first list; the second names other alone, off: the exec's table holds
        # each name the first list had.
        reply, calls, _log = self._decide(
            mcp_list_json(["home", "other"], off=["home"]), second={"stdout": mcp_list_json(["other"], off=["other"])}
        )

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table", "exec"])
        self.assertEqual(turned_off(calls[2]["argv"]), {"home", "other"})
        self.assertEqual(turned_off(self._exec_argv(calls)), {"home", "other"})

    def test_a_server_only_the_second_list_names_on_refuses_the_call(self) -> None:
        reply, calls, _log = self._decide(
            mcp_list_json(["home"]), second={"stdout": mcp_list_json(["home", "late"], off=["home"])}
        )

        self.assertIsInstance(reply, bridge.BridgeError)
        self.assertEqual((reply.status_code, reply.message), (REFUSED, SERVERS_STILL_ON))
        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table"], "codex exec ran")

    def test_a_server_whose_enabled_is_not_the_json_false_counts_as_on(self) -> None:
        entries = {
            "no key": {"name": KEPT_ON},
            "null": {"name": KEPT_ON, "enabled": None},
            "a string": {"name": KEPT_ON, "enabled": "false"},
            "0": {"name": KEPT_ON, "enabled": 0},
            "a list": {"name": KEPT_ON, "enabled": []},
            "an object": {"name": KEPT_ON, "enabled": {}},
        }
        for case, entry in entries.items():
            with self.subTest(enabled=case):
                listed = json.dumps([entry])
                reply, calls, _log = self._decide(listed, second={"stdout": listed})

                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual(reply.message, SERVERS_STILL_ON)
                self.assertEqual(self._kinds(calls), ["look", "list", "list with the table"])

    def test_a_name_listed_twice_is_on_where_one_of_its_entries_is(self) -> None:
        entries = [{"name": "home", "enabled": False}, {"name": "home", "enabled": True}]
        for order, listed in (("the entry off first", entries), ("the entry on first", entries[::-1])):
            with self.subTest(order=order):
                self.assertEqual(bridge.listed_mcp_servers(json.dumps(listed).encode("utf-8")), {"home": True})

    def test_a_list_that_fails_or_is_not_a_list_refuses_the_call(self) -> None:
        cases = {
            "exit 1 (a malformed config.toml)": dict(stdout="", exit_code=1, stderr=CONFIG_ERROR),
            "exit 1 with a list": dict(stdout=mcp_list_json(["home"]), exit_code=1),
            "exit 1 with stdout only": dict(stdout=FAILED_STDOUT, exit_code=1),
            "exit 1 with stdout and stderr": dict(stdout=FAILED_STDOUT, exit_code=1, stderr=f"Error: {STDERR_MARKER}"),
            "exit 1 with a stderr past what is logged": dict(stdout="", exit_code=1, stderr="e" * 2001),
            "not JSON": dict(stdout="Name  Command  sentinel-6c1e\nhome  python3"),
            "nothing": dict(stdout=""),
            "an object": dict(stdout=json.dumps({"name": "home"})),
            "a name that is not a string": dict(stdout=json.dumps([{"name": 7}])),
            "an entry without a name": dict(stdout=json.dumps([{"enabled": True}])),
            "an entry that is not an object": dict(stdout=json.dumps(["home"])),
        }
        for case, behaviour in cases.items():
            for which in ("first", "second"):
                with self.subTest(case=case, list=which):
                    if which == "first":
                        reply, calls, log = self._decide(**behaviour)
                    else:
                        second = {"stdout": behaviour["stdout"], "exit": behaviour.get("exit_code", 0)}
                        second["stderr"] = behaviour.get("stderr", "")
                        reply, calls, log = self._decide(mcp_list_json(["home"]), second=second)

                    self.assertIsInstance(reply, bridge.BridgeError)
                    self.assertEqual(
                        (reply.status_code, reply.message), (REFUSED, "codex mcp list failed; see the bridge log")
                    )
                    self.assertEqual([call for call in calls if "exec" in call["argv"]], [], "codex exec ran")
                    self.assertEqual(
                        len([call for call in calls if is_mcp_list(call["argv"])]), 1 + (which == "second")
                    )
                    self._only_stderr_and_the_stdouts_length_logged("codex mcp list", behaviour, log)

    def test_a_list_that_hangs_is_killed_at_the_calls_deadline_and_times_the_call_out(self) -> None:
        # As an exec that hangs does: the timed-out error itself, whichever run the call's one deadline passes in. The
        # fake leaves no process that holds its pipes, so no drain is owed: the call ends within 1.0 s of its deadline.
        timeout = 2.0
        for which in ("first", "second"):
            with self.subTest(list=which):
                started = time.monotonic()
                if which == "first":
                    reply, calls, log = self._decide("[]", sleep=60, request_timeout_seconds=timeout)
                else:
                    second = {"stdout": "[]", "sleep": 60}
                    reply, calls, log = self._decide(
                        mcp_list_json(["home"]), second=second, request_timeout_seconds=timeout
                    )
                elapsed = time.monotonic() - started

                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual((reply.status_code, reply.message), (504, "codex timed out after 2s"))
                self.assertLess(elapsed, timeout + 1.0)
                self.assertEqual([call for call in calls if "exec" in call["argv"]], [], "codex exec ran")
                self.assertEqual(self.service._in_flight, {})
                self.assertIn("mcp list", log)

    def test_a_list_that_hangs_after_slow_runs_gets_what_is_left_of_the_calls_time(self) -> None:
        # A machine too slow to reach the list under test within the call's 5 s fails this test on purpose.
        # The look takes 1.5 s of the call's 5 s (and, before a second list, the first list 1.5 s more): a list that
        # then hangs is killed at the call's one deadline, and the call ends within 1.0 s of it, less than each slow
        # run's 1.5 s. So a list given the whole timeout from its own start cannot pass, nor a second list given what
        # was left when the first one started.
        timeout, slow = 5.0, 1.5
        features = {"stdout": PINNED_FEATURES_LIST, "sleep": slow}
        runs = {"first": ["look", "list"], "second": ["look", "list", "list with the table"]}
        for which, kinds in runs.items():
            with self.subTest(list=which):
                started = time.monotonic()
                if which == "first":
                    reply, calls, _log = self._decide(
                        "[]", sleep=60, features=features, request_timeout_seconds=timeout
                    )
                else:
                    reply, calls, _log = self._decide(
                        mcp_list_json(["home"]),
                        sleep=slow,
                        second={"stdout": "[]", "sleep": 60},
                        features=features,
                        request_timeout_seconds=timeout,
                    )
                elapsed = time.monotonic() - started

                self.assertNotIn("exec", self._kinds(calls), "codex exec ran")
                self.assertEqual(self._kinds(calls), kinds, f"the {which} list, the run under test, was not reached")
                self.assertEqual((reply.status_code, reply.message), (504, "codex timed out after 5s"))
                self.assertLess(elapsed, timeout + 1.0)

    def test_the_look_and_the_lists_run_with_the_execs_environment(self) -> None:
        # A variable of the bridge's own environment that the call's environment does not pass.
        with patch.dict(os.environ, {"BRIDGE_ONLY_SENTINEL": "6c1e"}):
            reply, calls, _log = self._decide(mcp_list_json(["home"]))

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table", "exec"])
        environments = [call["env"] for call in calls]
        self.assertNotIn("BRIDGE_ONLY_SENTINEL", environments[-1])
        self.assertEqual(environments[:-1], [environments[-1]] * 3)

    def test_a_run_with_no_time_left_starts_no_codex_and_is_the_bridges_504(self) -> None:
        # The check is each run's own, so that a list, not only the exec, starts nothing once the call's time is up.
        service = bridge.CodexBridgeService(codex_path="codex", request_timeout_seconds=1.0)
        for timeout in (0, -1.0):
            with self.subTest(timeout=timeout):
                with (
                    patch.object(bridge.subprocess, "Popen", side_effect=AssertionError("codex was started")) as popen,
                    self.assertRaises(bridge.BridgeError) as caught,
                ):
                    service._run_codex(["codex", "mcp", "list", "--json"], b"", timeout=timeout, cwd=str(self.dir))

                popen.assert_not_called()
                self.assertIsInstance(caught.exception, bridge._CodexTimedOut)
                self.assertEqual(
                    (caught.exception.status_code, caught.exception.message), (504, "codex timed out after 1s")
                )
                self.assertEqual(service._in_flight, {})

    def test_a_run_that_prints_and_hangs_puts_nothing_it_printed_in_the_errors_text_cause_or_context(self) -> None:
        # A machine too slow to reach the run under test and have it print within the call's 2 s fails this on purpose.
        # What a run printed before it was killed: a list's server env values, an exec's start of a decision.
        marker = "printed-then-hung-6c1e"
        timeout = 2.0
        cases = {
            "the look": ("[]", {"features": {"stdout": marker, "hang": 60}}),
            "the first list": (marker, {"hang": 60}),
            "the second list": (mcp_list_json(["home"]), {"second": {"stdout": marker, "hang": 60}}),
            "the exec": ("[]", {"exec_stdout": marker, "exec_sleep": 60}),
        }
        for case, (stdout, behaviour) in cases.items():
            with self.subTest(run=case):
                reply, _calls, log = self._decide(stdout, request_timeout_seconds=timeout, **behaviour)

                self.assertIn(case, self._printed(), f"{case} was killed before it printed, or never ran")
                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual((reply.status_code, reply.message), (504, "codex timed out after 2s"))
                self.assertNotIn(marker, log)
                # On every platform: nothing is chained to the error. (Only POSIX's TimeoutExpired holds what a run
                # printed, so the marker checks below can go red there alone.)
                self.assertEqual(_reachable(reply), [reply])
                for error in _reachable(reply):
                    with self.subTest(run=case, error=type(error).__name__):
                        self.assertNotIn(marker, str(error))
                        named = {name: getattr(error, name, None) for name in ("output", "stdout", "stderr", "args")}
                        for name, value in {**named, **vars(error)}.items():
                            self.assertNotIn(marker, repr(value), name)

    def test_a_list_that_answers_as_the_calls_time_runs_out_starts_no_exec_and_times_out(self) -> None:
        # The exec has no time left: it is not started, and the call is answered as a timed-out run is.
        timeout = 1.0
        started: list[list[str]] = []

        def popen(command, **kwargs):
            if is_features_list(command):
                return NoServers(command, **kwargs)
            if is_mcp_list(command):
                return _ListAnsweringAtItsDeadline(command, **kwargs)
            started.append(command)
            return self._popen(command, **kwargs)

        service = bridge.CodexBridgeService(codex_path="codex", request_timeout_seconds=timeout)
        with (
            patch.object(bridge.subprocess, "Popen", side_effect=popen),
            self.assertRaises(bridge.BridgeError) as caught,
        ):
            service.handle_openai_decide(dict(PAYLOAD))

        self.assertEqual(started, [], "codex exec was started")
        self.assertEqual((caught.exception.status_code, caught.exception.message), (504, "codex timed out after 1s"))
        self.assertEqual(service._in_flight, {})

    def test_the_look_the_lists_and_the_exec_share_the_calls_deadline(self) -> None:
        # The look and the second list take 1.5 s each; the exec does not answer: it gets what the three left, and the
        # call ends within 2 s of the one deadline, less than the 3 s the look and the second list took, so an exec
        # given a whole timeout of its own cannot pass.
        timeout = 6.0
        started = time.monotonic()
        reply, calls, _log = self._decide(
            mcp_list_json(["home"]),
            features={"stdout": PINNED_FEATURES_LIST, "sleep": 1.5},
            second={"stdout": mcp_list_json(["home"], off=["home"]), "sleep": 1.5},
            exec_sleep=60,
            request_timeout_seconds=timeout,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(self._kinds(calls), ["look", "list", "list with the table", "exec"])
        self.assertEqual((reply.status_code, reply.message), (504, "codex timed out after 6s"))
        self.assertLess(elapsed, timeout + 2.0)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeFeaturesLookTests(_BridgeAgainstTheFake):
    """The look at `codex <the call's -c flags> features list` the bridge makes before its lists."""

    def test_a_feature_of_the_call_listed_on_refuses_the_call_before_any_list(self) -> None:
        for feature in LOOKED_AT:
            with self.subTest(feature=feature):
                reply, calls, log = self._decide("[]", features={"stdout": features_list([feature])})

                # ONE_FEATURE_ON is the refusal written out for shell_tool: the same sentence, for this feature.
                expected = ONE_FEATURE_ON.replace("shell_tool", feature)
                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual((reply.status_code, reply.message), (REFUSED, expected))
                self.assertEqual(self._kinds(calls), ["look"])
                self.assertIn(expected, log)

    def test_the_refusal_says_where_a_configuration_can_turn_a_feature_on(self) -> None:
        cases = (
            (["shell_tool"], ONE_FEATURE_ON),
            (["shell_tool", "apps"], TWO_FEATURES_ON),
            (LOOKED_AT, EVERY_FEATURE_ON),
        )
        for on, said in cases:
            with self.subTest(on=on):
                reply, _calls, log = self._decide("[]", features={"stdout": features_list(on)})

                self.assertEqual(reply.message, said)
                self.assertEqual(log, f"[codex-bridge] {said}; the Codex call was not made\n")  # logged whole

    def test_the_refusal_names_each_feature_listed_on_and_nothing_else_codex_printed(self) -> None:
        listed = features_list(["codex_hooks", "fast_mode", "shell_tool", "apps"]) + "sentinel_6c1e  stable  true\n"
        reply, _calls, log = self._decide("[]", features={"stdout": listed, "stderr": "sentinel-6c1e"})

        self.assertEqual(reply.message, THREE_FEATURES_ON)  # in the call's own order
        self.assertNotIn("sentinel", log)

    def test_a_features_list_with_each_feature_of_the_call_off_or_missing_goes_on(self) -> None:
        cases = {
            "the pinned Codex": PINNED_FEATURES_LIST,
            "a newer Codex": NEWER_FEATURES_LIST,
            "unified_exec on": features_list(["unified_exec"]),
            "no codex_hooks row": "".join(
                row + "\n" for row in PINNED_FEATURES_LIST.splitlines() if not row.startswith("codex_hooks ")
            ),
            "blank lines and CRLF": BLANK_LINES_AND_CRLF,
        }
        for case, listed in cases.items():
            with self.subTest(case=case):
                reply, calls, _log = self._decide("[]", features={"stdout": listed})

                self.assertEqual(reply["raw_text"], '{"action": "done"}')
                self.assertEqual(self._kinds(calls), ["look", "list", "exec"])

    def test_of_two_rows_for_one_feature_the_true_one_wins(self) -> None:
        true_row = "shell_tool  stable  true\n"  # beside the pinned list's own row of shell_tool, which reads false
        orders = {
            "the false row first": PINNED_FEATURES_LIST + true_row,
            "the true row first": true_row + PINNED_FEATURES_LIST,
        }
        for order, listed in orders.items():
            with self.subTest(order=order):
                self.assertEqual(bridge.features_on(listed.encode("utf-8")), ["shell_tool"])

    def test_a_look_that_fails_or_is_not_a_features_list_refuses_the_call(self) -> None:
        cases = {
            "exit 1 (a malformed config.toml)": {"stdout": "", "exit": 1, "stderr": CONFIG_ERROR},
            "exit 1 with a list": {"stdout": PINNED_FEATURES_LIST, "exit": 1},
            "exit 1 with stdout only": {"stdout": FAILED_STDOUT, "exit": 1},
            "nothing": {"stdout": ""},
            "a row that ends in neither word": {"stdout": "shell_tool  stable  sentinel-6c1e\n" + PINNED_FEATURES_LIST},
            "a line of two words": {"stdout": "sentinel_6c1e  false\n" + PINNED_FEATURES_LIST},
            "an error among the rows": {"stdout": PINNED_FEATURES_LIST + "Error: failed\n"},
            # In the stage word of a row that reads false, as a lone surrogate the fake prints as the byte.
            "a byte that is no UTF-8 in a stage word": {
                "stdout": "".join(
                    "fast_mode  st\udcffable  false\n" if row.startswith("fast_mode ") else row + "\n"
                    for row in PINNED_FEATURES_LIST.splitlines()
                )
            },
        }
        for case, features in cases.items():
            with self.subTest(case=case):
                reply, calls, log = self._decide("[]", features=features)

                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual((reply.status_code, reply.message), LOOK_FAILED)
                self.assertEqual(self._kinds(calls), ["look"])
                # As a failed server list's.
                self._only_stderr_and_the_stdouts_length_logged("codex features list", features, log)
        self.assertIsNone(bridge.features_on(PINNED_FEATURES_LIST.encode("utf-8") + b"\xff  stable  false\n"))

    def test_a_line_whose_first_word_is_no_feature_name_refuses_the_call(self) -> None:
        cases = {
            **FIRST_WORD_NO_NAME,
            "a byte in the name that is no UTF-8": NOT_UTF8_ROW.decode("utf-8", "surrogateescape"),
        }
        for case, stdout in cases.items():
            with self.subTest(case=case):
                reply, calls, _log = self._decide("[]", features={"stdout": stdout})

                self.assertIsInstance(reply, bridge.BridgeError)
                self.assertEqual((reply.status_code, reply.message), LOOK_FAILED)
                self.assertEqual(self._kinds(calls), ["look"])

    def test_a_look_that_hangs_is_killed_at_the_calls_deadline_and_times_the_call_out(self) -> None:
        # The fake leaves no process that holds its pipes, so no drain is owed: the call ends within 1.0 s of its
        # deadline.
        timeout = 2.0
        started = time.monotonic()
        reply, calls, log = self._decide(
            "[]", features={"stdout": PINNED_FEATURES_LIST, "sleep": 60}, request_timeout_seconds=timeout
        )
        elapsed = time.monotonic() - started

        self.assertIsInstance(reply, bridge.BridgeError)
        self.assertEqual((reply.status_code, reply.message), (504, "codex timed out after 2s"))
        self.assertIn("[codex-bridge] codex features list: codex timed out after 2s", log)
        self.assertLess(elapsed, timeout + 1.0)
        self.assertEqual(self._kinds(calls), ["look"])
        self.assertEqual(self.service._in_flight, {})

    def test_a_look_the_bridges_stop_kills_is_answered_as_the_stop(self) -> None:
        # kill_in_flight, as main() calls it when the server stops: the look it kills exits non-zero, and the call is
        # answered as one that reaches codex after the sweep is.
        behaviour = {"first": {"stdout": "[]"}, "second": None, "kept_on": [], "exec_sleep": 0}
        behaviour["features"] = {"stdout": PINNED_FEATURES_LIST, "sleep": 60}
        (self.dir / "behaviour.json").write_text(json.dumps(behaviour), encoding="utf-8")
        service = bridge.CodexBridgeService(codex_path="codex", request_timeout_seconds=30.0)
        replies: list[object] = []

        def decide() -> None:
            try:
                replies.append(service.handle_openai_decide(dict(PAYLOAD)))
            except bridge.BridgeError as exc:
                replies.append(exc)

        calls = self.dir / "calls.jsonl"
        with patch.object(bridge.subprocess, "Popen", side_effect=self._popen), redirect_stderr(io.StringIO()):
            worker = threading.Thread(target=decide, daemon=True)
            worker.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (calls.exists() and calls.read_text(encoding="utf-8")):
                time.sleep(0.05)  # the look has started and noted its call
            self.assertEqual(service.kill_in_flight(), 1)
            worker.join(10)

        self.assertFalse(worker.is_alive())
        (reply,) = replies
        self.assertIsInstance(reply, bridge.BridgeError)
        self.assertEqual((reply.status_code, reply.message), (503, "the bridge is stopping"))
        lines = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(self._kinds([json.loads(line) for line in lines]), ["look"])

    def test_a_feature_added_to_the_bridges_settings_is_looked_at(self) -> None:
        source = BRIDGE_SCRIPT.read_text(encoding="utf-8")
        line = '    "features.multi_agent=false",\n'
        added = source.replace(line, line + '    "features.planted=false",\n', 1)
        self.assertNotEqual(added, source)
        copy = self.dir / "codex_host_bridge_copy.py"
        copy.write_text(added, encoding="utf-8")
        module = load_bridge("codex_host_bridge_with_a_feature_added", copy)

        self.assertIn("planted", module.LOOKED_AT_FEATURES)
        self.assertEqual(module.features_on((PINNED_FEATURES_LIST + "planted  stable  true\n").encode()), ["planted"])
        self.assertEqual(module.features_on((PINNED_FEATURES_LIST + "planted  stable  false\n").encode()), [])


def _reachable(error: BaseException) -> list[BaseException]:
    """``error`` and each exception reachable from it through __cause__, __context__, its args and an attribute of its
    own, a list, tuple, set or dict among them looked into (an object of another kind is not)."""
    found: list[BaseException] = []
    seen: set[int] = set()
    pending: list[object] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseException):
            found.append(current)
            pending += [current.__cause__, current.__context__, current.args, *vars(current).values()]
        elif isinstance(current, dict):
            pending += [*current, *current.values()]
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending += current
    return found


class _ListAnsweringAtItsDeadline(NoServers):
    """`codex mcp list --json` that names no server 50 ms after the time it is given has run out."""

    def communicate(self, input=None, timeout=None):
        time.sleep(timeout + 0.05)
        return super().communicate(input, timeout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
