#!/usr/bin/env python3
"""Tests for the pipe handling of fenced() in scripts/render_report.py (task 9.8).

GitHub splits a table row into cells before it reads the inline markup inside them, so a
bare pipe ends a cell even between backticks. A pipe that follows a backslash does not, and
the renderer hands that backslash back out of the cell. These tests pin both halves: the
helper's own output, and the row count and cell count of a rendered table whose rule id and
account id carry a pipe, from a report and from a hand-edited state marker.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_report.py"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

import render_report  # noqa: E402

PIN = "da656733fff839d5cf20f795460f4365a7254257"
ACCOUNT = "GBQHXYDEJD6NEIUQ5NYDBVNTRCACJXMLO32JUQHBGO524NMVYOMIN5CP"
MARKER_RE = re.compile(r"<!-- posture-watch:state v1 ([A-Za-z0-9_-]*) -->")
# A pipe that follows a backslash stays inside its cell; every other pipe ends one.
CELL_BOUNDARY = re.compile(r"(?<!\\)\|")
HOSTILE_RULES = ("flag|drift", "a|b|c", "|leading", "trailing|", "back\\|slash", "tick`|`pipe")


def cells(row: str) -> list[str]:
    """Split one table row the way GitHub does, dropping the two outer pipes."""
    inner = row.strip()
    if not (inner.startswith("|") and inner.endswith("|")):
        raise AssertionError(f"not a table row: {row!r}")
    return [cell.strip() for cell in CELL_BOUNDARY.split(inner[1:-1])]


def table(body: str, heading: str) -> list[str]:
    """The data rows under one heading, without the header and the delimiter row."""
    section = body.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return rows[2:]


def decode_marker(body: str) -> list[dict[str, Any]]:
    match = MARKER_RE.search(body)
    if match is None:
        raise AssertionError("the body carries no state marker")
    payload = match.group(1)
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(decoded, list):
        raise AssertionError("the state marker does not hold a list")
    return decoded


def finding(rule: str, account: str, index: int) -> dict[str, Any]:
    return {
        "rule_id": rule,
        "severity": "high",
        "account": account,
        "message": "a finding whose rule id carries a pipe",
        "evidence": "x",
        "fingerprint": f"{index:064x}",
        "accepted": False,
    }


class TestFencedHelper(unittest.TestCase):
    def test_a_pipe_is_backslash_escaped_inside_the_backticks(self) -> None:
        self.assertEqual(render_report.fenced("a|b"), "`a\\|b`")

    def test_every_pipe_is_escaped_not_only_the_first(self) -> None:
        self.assertEqual(render_report.fenced("|a||b|"), "`\\|a\\|\\|b\\|`")

    def test_a_normal_rule_id_is_unchanged(self) -> None:
        for rule in ("acct-missing", "flag-undeclared-clawback", "signer_weight.low", ACCOUNT):
            with self.subTest(rule=rule):
                self.assertEqual(render_report.fenced(rule), f"`{rule}`")

    def test_every_rule_id_in_the_captured_reports_is_unchanged(self) -> None:
        seen = 0
        for path in sorted(FIXTURES.glob("report-*.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            for account in report["accounts"]:
                for entry in account.get("findings") or []:
                    seen += 1
                    with self.subTest(fixture=path.name, rule=entry["rule_id"]):
                        self.assertEqual(
                            render_report.fenced(entry["rule_id"]), f"`{entry['rule_id']}`"
                        )
        self.assertGreater(seen, 0, "no captured report carries a finding to compare")

    def test_a_backtick_is_still_neutralised_next_to_a_pipe(self) -> None:
        self.assertEqual(render_report.fenced("a`|`b"), "`a&#96;\\|&#96;b`")

    def test_no_pipe_in_the_output_can_end_a_cell(self) -> None:
        for value in (*HOSTILE_RULES, "||", "\\", "\\\\|", "a\\\\|b"):
            with self.subTest(value=value):
                row = f"| {render_report.fenced(value)} |"
                self.assertEqual(len(cells(row)), 1, row)

    def test_a_value_that_is_not_a_string_is_still_fenced(self) -> None:
        self.assertEqual(render_report.fenced(None), "`None`")
        self.assertEqual(render_report.fenced(12), "`12`")


class TestRenderedTables(unittest.TestCase):
    """Runs the script the way the Action does, as a subprocess over temp files."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.tmp = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def render(self, report: Path, previous: Path | None = None) -> tuple[int, str, str]:
        body = self.tmp / f"body-{report.stem}.md"
        argv = [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report),
            "--out",
            str(body),
            "--as-of",
            "2026-11-03",
            "--config",
            "policy.toml",
            "--gauge-ref",
            PIN,
            "--exit-code",
            str(json.loads(report.read_text(encoding="utf-8"))["exit_code"]),
        ]
        if previous is not None:
            argv += ["--previous-body", str(previous)]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        text = body.read_text(encoding="utf-8") if body.exists() else ""
        return done.returncode, text, done.stderr

    def hostile_report(self, account: str = ACCOUNT) -> Path:
        report = json.loads((FIXTURES / "report-clean.json").read_text(encoding="utf-8"))
        report["exit_code"] = 1
        report["accounts"][0]["snapshot"]["id"] = account
        report["accounts"][0]["findings"] = [
            finding(rule, account, index) for index, rule in enumerate(HOSTILE_RULES, start=1)
        ]
        path = self.tmp / "pipes.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_a_rule_id_with_a_pipe_keeps_one_row_of_five_cells_per_finding(self) -> None:
        code, body, err = self.render(self.hostile_report())
        self.assertEqual(code, 0, err)
        for heading in ("New since the previous run", "Current findings"):
            rows = table(body, heading)
            self.assertEqual(len(rows), len(HOSTILE_RULES), heading)
            for row in rows:
                with self.subTest(heading=heading, row=row):
                    self.assertEqual(len(cells(row)), 5, row)
            rules = sorted(cells(row)[1] for row in rows)
            self.assertEqual(rules, sorted(render_report.fenced(rule) for rule in HOSTILE_RULES))

    def test_the_marker_still_decodes_to_the_rule_ids_the_report_gave(self) -> None:
        code, body, err = self.render(self.hostile_report())
        self.assertEqual(code, 0, err)
        state = decode_marker(body)
        self.assertEqual(len(state), len(HOSTILE_RULES))
        self.assertEqual(sorted(entry["rule"] for entry in state), sorted(HOSTILE_RULES))

    def test_an_account_id_with_a_pipe_keeps_both_tables_in_shape(self) -> None:
        account = "G|not|an|account"
        code, body, err = self.render(self.hostile_report(account))
        self.assertEqual(code, 0, err)
        for row in table(body, "Current findings"):
            with self.subTest(row=row):
                found = cells(row)
                self.assertEqual(len(found), 5, row)
                self.assertEqual(found[2], render_report.fenced(account))
        accounts = table(body, "Accounts asked about")
        self.assertEqual(len(accounts), 1)
        self.assertEqual(len(cells(accounts[0])), 8, accounts[0])
        self.assertEqual(cells(accounts[0])[0], render_report.fenced(account))
        self.assertEqual({entry["account"] for entry in decode_marker(body)}, {account})

    def test_a_hand_edited_marker_cannot_add_a_column_to_the_resolved_table(self) -> None:
        # decode_state takes any string for rule and account, so the previous body is a way
        # to put a value in front of fenced() that no report produced.
        forged = [
            {
                "account": "G|forged",
                "fingerprint": "f" * 64,
                "message": "left by a hand-edited marker",
                "rule": "x|y|z",
                "severity": "high",
            }
        ]
        payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        previous = self.tmp / "previous.md"
        previous.write_text(f"<!-- posture-watch:state v1 {payload} -->\n", encoding="utf-8")
        code, body, err = self.render(FIXTURES / "report-clean.json", previous)
        self.assertEqual(code, 0, err)
        rows = table(body, "Resolved since the previous run")
        self.assertEqual(len(rows), 1)
        found = cells(rows[0])
        self.assertEqual(len(found), 5, rows[0])
        self.assertEqual(found[1], "`x\\|y\\|z`")
        self.assertEqual(found[2], "`G\\|forged`")
        self.assertEqual(decode_marker(body), [])


if __name__ == "__main__":
    unittest.main()
