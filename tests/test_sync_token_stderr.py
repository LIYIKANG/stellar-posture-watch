#!/usr/bin/env python3
"""Tests that no exit path of scripts/sync_issue.py lets the token out (task 9.9).

`tests/test_sync_issue.py` checks one failure message: three 500s with one bearer value.
This module drives every way `main()` can end, with the token inside whatever the
transport says, and reads the three places a step can leak through: stdout, stderr and
the file `GITHUB_OUTPUT` names.

Nothing here opens a socket. The transport is the same seam the other module stubs, and
the token is assembled at run time so no token-shaped literal sits in the repository.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sync_issue  # noqa: E402

# The shape redact() already knows for an installation token: `ghs_` and 36 more characters.
TOKEN = "ghs_" + "t0Ken" * 7 + "Z"
URL = "https://api.example.test/x"
MARKER_BODY = f"an issue body\n\n{sync_issue.MARKER_PREFIX}W30= -->\n"

# Four ways a server, a proxy or a library can hand the token back in a message.
SHAPES: tuple[str, ...] = (
    "bad credentials for {token}",
    "Authorization: Bearer {token}",
    "retry with token={token} later",
    '{{"message": "denied", "token": "{token}"}}',
)


def http(code: int, message: str, retry_after: str | None = None) -> urllib.error.HTTPError:
    """An HTTP failure that carries `message` in both its reason phrase and its body."""
    headers = Message()
    headers["Content-Type"] = "application/json"
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, message, headers, io.BytesIO(message.encode()))


@dataclass(frozen=True)
class Case:
    """One way the transport can fail, as the list of outcomes it replays."""

    name: str
    outcomes: Callable[[str], list[Any]]
    #: True when the failure message repeats what the transport said, so the mask must show.
    quoted: bool = True
    #: A 200 that is not JSON only stops the calls that parse their answer.
    actions: tuple[str, ...] = ("find", "create", "update")


CASES: tuple[Case, ...] = (
    Case("302 refused", lambda m: [http(302, m)]),
    Case("401", lambda m: [http(401, m)]),
    Case("403", lambda m: [http(403, m)]),
    Case("403 with Retry-After, three times", lambda m: [http(403, m, "1") for _ in range(3)]),
    Case("404", lambda m: [http(404, m)]),
    Case("422", lambda m: [http(422, m)]),
    Case("429 told to wait too long", lambda m: [http(429, m, "3600")]),
    Case("429 then 401", lambda m: [http(429, m, "1"), http(401, m)]),
    Case("429 three times", lambda m: [http(429, m, "1") for _ in range(3)]),
    Case("500 three times", lambda m: [http(500, m) for _ in range(3)]),
    Case("URLError", lambda m: [urllib.error.URLError(m) for _ in range(3)]),
    Case("TimeoutError", lambda m: [TimeoutError(m) for _ in range(3)]),
    Case("OSError", lambda m: [OSError(m) for _ in range(3)]),
    Case(
        "200 that is not JSON",
        lambda m: [(200, f"<html>{m}</html>".encode())],
        False,
        ("find", "create"),
    ),
)


class Stub:
    """A transport that replays a script of outcomes and records what it was sent."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.script:
            raise AssertionError(f"the stub ran out of scripted outcomes at {method} {url}")
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, payload = outcome
        return int(status), bytes(payload)


@dataclass(frozen=True)
class Run:
    """Everything one call of main() left behind."""

    code: int
    stdout: str
    stderr: str
    output_file: str
    stub: Stub

    def surfaces(self) -> dict[str, str]:
        return {"stdout": self.stdout, "stderr": self.stderr, "GITHUB_OUTPUT": self.output_file}


class Harness(unittest.TestCase):
    """Runs main() in process with a stubbed transport, a token and a step output file."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.tmp = Path(self._directory.name)
        self._runs = 0
        sleeper = mock.patch.object(sync_issue.time, "sleep", lambda seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def file(self, name: str, text: str) -> str:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def summary(self, **overrides: Any) -> str:
        data: dict[str, Any] = {
            "changed": True,
            "new": 1,
            "resolved": 0,
            "open": 1,
            "as_of": "2026-11-03",
        }
        data.update(overrides)
        return self.file("summary.json", json.dumps(data))

    def argv(self, action: str) -> list[str]:
        common = ["--label", "account-posture", "--title", "Account posture watch"]
        if action == "find":
            return ["find", *common, "--previous-body-out", str(self.tmp / "previous.md")]
        argv = ["write", *common, "--body", self.file("body.md", "the rendered body")]
        argv += ["--summary", self.summary()]
        if action == "update":
            argv += ["--number", "7"]
        return argv

    def run_main(
        self,
        argv: list[str],
        script: list[Any] | None = None,
        *,
        env: dict[str, str] | None = None,
        output: Path | None = None,
    ) -> Run:
        self._runs += 1
        target = output if output is not None else self.tmp / f"github-output-{self._runs}"
        environment = {
            "GITHUB_API_URL": "https://api.example.test",
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_OUTPUT": str(target),
            "POSTURE_WATCH_TOKEN": TOKEN,
        }
        environment.update(env or {})
        stub = Stub(script or [])
        out = io.StringIO()
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(sync_issue, "TRANSPORT", stub),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = sync_issue.main(argv)
        written = target.read_text(encoding="utf-8") if target.is_file() else ""
        return Run(code, out.getvalue(), err.getvalue(), written, stub)

    def leaks(self, run: Run) -> list[str]:
        """The names of the surfaces the token reached."""
        return [name for name, text in run.surfaces().items() if TOKEN in text]

    def assert_no_token(self, run: Run) -> None:
        self.assertEqual(self.leaks(run), [], run.surfaces())


class TestTransportFailures(Harness):
    def test_no_failure_of_the_transport_prints_the_token(self) -> None:
        ran = 0
        for case in CASES:
            for shape in SHAPES:
                message = shape.format(token=TOKEN)
                for action in case.actions:
                    with self.subTest(case=case.name, shape=shape, action=action):
                        run = self.run_main(self.argv(action), case.outcomes(message))
                        ran += 1
                        self.assertEqual(run.code, 3, run.stderr)
                        self.assert_no_token(run)
                        self.assertTrue(run.stderr.startswith("sync_issue: "), run.stderr)
                        if case.quoted:
                            self.assertIn("[redacted]", run.stderr)
                        else:
                            self.assertIn("is not JSON", run.stderr)
                        # The token did go out on the wire, so a clean log is not an
                        # accident of the stub never seeing it.
                        self.assertEqual(
                            run.stub.calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}"
                        )
                        self.assertEqual(run.stub.script, [], "an outcome was never reached")
        self.assertEqual(ran, sum(len(case.actions) for case in CASES) * len(SHAPES))

    def test_a_token_cut_by_the_detail_limit_is_not_printed_in_part(self) -> None:
        # A failure body is quoted up to 200 characters. A token that starts before that
        # mark and ends after it must not come out as a prefix redact() no longer knows.
        for lead in range(150, 200):
            message = "x" * lead + " " + TOKEN
            for code in (401, 403, 404, 422):
                with self.subTest(lead=lead, code=code):
                    failure = urllib.error.HTTPError(
                        URL, code, "denied", Message(), io.BytesIO(message.encode())
                    )
                    run = self.run_main(self.argv("find"), [failure])
                    self.assertEqual(run.code, 3, run.stderr)
                    for size in range(8, len(TOKEN) + 1):
                        self.assertNotIn(TOKEN[:size], run.stderr, f"{size} characters of it")
                    self.assert_no_token(run)

    def test_the_check_sees_a_leak_when_redact_is_taken_away(self) -> None:
        # The proof that the table above can fail: with redact() replaced by the identity,
        # which is what printing the detail directly would be, the same check reports stderr.
        with mock.patch.object(sync_issue, "redact", lambda text: text):
            for case in CASES:
                if not case.quoted:
                    continue
                with self.subTest(case=case.name):
                    message = SHAPES[0].format(token=TOKEN)
                    run = self.run_main(self.argv("find"), case.outcomes(message))
                    self.assertEqual(run.code, 3)
                    self.assertEqual(self.leaks(run), ["stderr"])


class TestExitsBeforeTheFirstCall(Harness):
    def test_an_empty_token_names_the_variable_and_prints_no_other_secret(self) -> None:
        # POSTURE_WATCH_TOKEN is empty, but a runner still holds the token under other
        # names, and a message built from the environment would print them.
        env = {"POSTURE_WATCH_TOKEN": "", "GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}
        for action in ("find", "create", "update"):
            with self.subTest(action=action):
                run = self.run_main(self.argv(action), env=env)
                self.assertEqual(run.code, 2)
                self.assertIn("POSTURE_WATCH_TOKEN is empty", run.stderr)
                self.assert_no_token(run)
                self.assertEqual(run.stub.calls, [])
                self.assertEqual(run.output_file, "")

    def test_a_refused_argument_that_carries_the_token_is_masked(self) -> None:
        for flag in ("--owner", "--repo", "--label"):
            for value in (f"{TOKEN}/x", f"a b {TOKEN}", f"token={TOKEN}?", "x" * 101 + TOKEN):
                with self.subTest(flag=flag, value=value):
                    argv = ["find", "--title", "Account posture watch"]
                    argv += ["--previous-body-out", str(self.tmp / "previous.md")]
                    fields = {"--owner": "acme", "--repo": "widgets", "--label": "posture"}
                    fields[flag] = value
                    for name, content in fields.items():
                        argv += [name, content]
                    run = self.run_main(argv)
                    self.assertEqual(run.code, 2, run.stderr)
                    self.assertIn(f"sync_issue: refused: {flag[2:]} ", run.stderr)
                    self.assertIn("[redacted]", run.stderr)
                    self.assert_no_token(run)
                    self.assertEqual(run.stub.calls, [])

    def test_a_refusal_that_quotes_nothing_prints_nothing_of_the_token(self) -> None:
        find = ["find", "--label", "account-posture", "--title", "Account posture watch"]
        handover = ["--previous-body-out", str(self.tmp / "previous.md")]
        cases: dict[str, tuple[list[str], dict[str, str]]] = {
            "no slash in GITHUB_REPOSITORY": ([*find, *handover], {"GITHUB_REPOSITORY": TOKEN}),
            "a control character in the title": (
                ["find", "--label", "account-posture", "--title", f"{TOKEN}\x07", *handover],
                {},
            ),
            "find with nowhere to hand the body": (find, {}),
            "a number below one": ([*self.argv("create"), "--number", "0"], {}),
        }
        for name, (argv, env) in cases.items():
            with self.subTest(case=name):
                run = self.run_main(argv, env=env)
                self.assertEqual(run.code, 2, run.stderr)
                self.assertIn("sync_issue: refused: ", run.stderr)
                self.assert_no_token(run)
                self.assertEqual(run.stub.calls, [])

    def test_a_missing_or_unreadable_summary_prints_nothing_of_the_token(self) -> None:
        body = self.file("body.md", "the rendered body")
        common = ["write", "--label", "account-posture", "--title", "Account posture watch"]
        cases: dict[str, list[str]] = {
            "no --summary at all": [*common, "--body", body],
            "no --body at all": [*common, "--summary", self.summary()],
            "a summary path with no file": [
                *common,
                "--body",
                body,
                "--summary",
                str(self.tmp / "absent.json"),
            ],
            "a summary that is not JSON": [
                *common,
                "--body",
                body,
                "--summary",
                self.file("broken.json", f"Bearer {TOKEN}"),
            ],
            "a summary that is not an object": [
                *common,
                "--body",
                body,
                "--summary",
                self.file("list.json", json.dumps([TOKEN])),
            ],
            "a summary with a key missing": [
                *common,
                "--body",
                body,
                "--summary",
                self.file("partial.json", json.dumps({"changed": True, "note": TOKEN})),
            ],
            "a body path with no file": [
                *common,
                "--body",
                str(self.tmp / "absent.md"),
                "--summary",
                self.summary(),
            ],
        }
        for name, argv in cases.items():
            with self.subTest(case=name):
                run = self.run_main(argv)
                self.assertEqual(run.code, 3, run.stderr)
                self.assertTrue(run.stderr.startswith("sync_issue: "), run.stderr)
                self.assert_no_token(run)
                self.assertEqual(run.stub.calls, [])

    def test_a_step_output_that_cannot_be_written_prints_nothing_of_the_token(self) -> None:
        # GITHUB_OUTPUT names a directory, so emit() fails on the first output it publishes.
        target = self.tmp / "not-a-file"
        target.mkdir()
        run = self.run_main(self.argv("find"), [(200, b"[]")], output=target)
        self.assertEqual(run.code, 3, run.stderr)
        self.assertTrue(run.stderr.startswith("sync_issue: "), run.stderr)
        self.assert_no_token(run)


class TestExitsThatSucceed(Harness):
    def test_no_successful_run_prints_or_publishes_the_token(self) -> None:
        issue = json.dumps([{"number": 7, "title": "Account posture watch", "body": MARKER_BODY}])
        created = json.dumps({"number": 9}).encode()
        cases: dict[str, tuple[list[str], list[Any], str]] = {
            "find, nothing open": (self.argv("find"), [(200, b"[]")], "found=false\n"),
            "find, the marked issue": (self.argv("find"), [(200, issue.encode())], "found=true\n"),
            "write, opening the baseline": (
                self.argv("create"),
                [(201, created)],
                "issue-number=9\n",
            ),
            "write, a dry run": ([*self.argv("create"), "--dry-run"], [], "issue-number=\n"),
            "write, body and comment": (
                self.argv("update"),
                [(200, b"{}"), (201, b"{}")],
                "issue-number=7\n",
            ),
        }
        for name, (argv, script, expected) in cases.items():
            with self.subTest(case=name):
                run = self.run_main(argv, script)
                self.assertEqual(run.code, 0, run.stderr)
                self.assertEqual(run.stderr, "")
                self.assertIn(expected, run.output_file)
                self.assert_no_token(run)
                for call in run.stub.calls:
                    self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
                    self.assertNotIn(TOKEN, call["url"])
                    self.assertNotIn(TOKEN.encode(), call["body"] or b"")
        previous = self.tmp / "previous.md"
        self.assertNotIn(TOKEN, previous.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
