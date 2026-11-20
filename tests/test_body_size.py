#!/usr/bin/env python3
"""Tests for the body ceiling in scripts/render_report.py (task 9.11).

An issue body can hold 65,536 characters. Until this task the renderer wrote whatever the
report came to and left the fitting to `sync_issue.py`, which trims one table after the
fact and says nothing in the body about the rows it dropped. These tests build reports that
render past the ceiling out of `report-findings.json` and read what comes back: the length,
the notice, the rows that stayed, and a state marker that still decodes to what it carried.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_report.py"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

import render_report  # noqa: E402
import sync_issue  # noqa: E402

PIN = "da656733fff839d5cf20f795460f4365a7254257"
CAPTURED = "b03faa6106f2405560196e0cad9e0bb74cab9c08d798877c0d795019bddd43d6"
MARKER_RE = re.compile(r"<!-- posture-watch:state v1 ([A-Za-z0-9_-]*) -->")
NOTICE = "**This body was shortened to fit.**"
LEFT_OUT = re.compile(r"so (\d+) finding rows? (?:was|were) left out")
PER_TABLE = re.compile(r"(\d+) of (\d+) under ([A-Z][a-z]+(?: [a-z]+)*)")
DROPPED_ROW = re.compile(r"^\| \.\.\. \| \.\.\. \| \.\.\. \| \.\.\. \| (\d+) more left out to fit")
HEADINGS = {
    "new": "New since the previous run",
    "resolved": "Resolved since the previous run",
    "current": "Current findings",
    "accepted": "Accepted in the policy",
}


def decode_marker(body: str) -> list[dict[str, Any]]:
    match = MARKER_RE.search(body)
    if match is None:
        raise AssertionError("the body carries no state marker")
    payload = match.group(1)
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(decoded, list):
        raise AssertionError("the state marker does not hold a list")
    return decoded


def table(body: str, name: str) -> tuple[list[str], int]:
    """The finding rows shown under one heading, and how many its last row says it left out."""
    section = body.split(f"## {HEADINGS[name]}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")][2:]
    if rows and (match := DROPPED_ROW.match(rows[-1])):
        return rows[:-1], int(match.group(1))
    return rows, 0


class Renderer(unittest.TestCase):
    """Runs the script the way the Action does, as a subprocess over temp files."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.tmp = Path(self._directory.name)
        self._calls = 0

    def tearDown(self) -> None:
        self._directory.cleanup()

    def report(self, *, opened: int = 0, accepted: int = 0, accounts: int = 0) -> Path:
        """`report-findings.json` with copies of its one finding, and of its one read account."""
        report = json.loads((FIXTURES / "report-findings.json").read_text(encoding="utf-8"))
        absent = report["accounts"][1]
        captured = absent["findings"][0]
        for index in range(opened + accepted):
            extra = copy.deepcopy(captured)
            extra["fingerprint"] = f"{index + 1:064x}"
            extra["accepted"] = index >= opened
            absent["findings"].append(extra)
        for index in range(accounts):
            extra_account = copy.deepcopy(report["accounts"][0])
            extra_account["snapshot"]["id"] = f"G{index:055d}"
            report["accounts"].append(extra_account)
        self._calls += 1
        path = self.tmp / f"large-{self._calls}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def render(
        self, report: Path, previous: Path | None = None
    ) -> tuple[int, str, dict[str, Any], str]:
        self._calls += 1
        body = self.tmp / f"body-{self._calls}.md"
        summary = self.tmp / f"summary-{self._calls}.json"
        argv = [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report),
            "--out",
            str(body),
            "--summary-out",
            str(summary),
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
        data: dict[str, Any] = {}
        if summary.exists() and done.returncode == 0:
            data = json.loads(summary.read_text(encoding="utf-8"))
        text = body.read_text(encoding="utf-8") if body.exists() else ""
        return done.returncode, text, data, done.stderr

    def arguments(self, report: Path, previous: Path | None = None) -> argparse.Namespace:
        """The same call as render(), for the tests that run the renderer in process."""
        argv = [
            "--report",
            str(report),
            "--out",
            str(self.tmp / "unused.md"),
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
        return render_report.parse_args(argv)

    def assert_notice_matches_the_tables(self, body: str, totals: dict[str, int]) -> int:
        """Every count the notice gives is the count its table shows. Returns the rows dropped."""
        self.assertEqual(body.count(NOTICE), 1)
        notice = next(line for line in body.splitlines() if line.startswith(NOTICE))
        named = {title: (int(left), int(of)) for left, of, title in PER_TABLE.findall(notice)}
        dropped_total = 0
        for name, total in totals.items():
            if total == 0:
                continue
            rows, dropped = table(body, name)
            self.assertEqual(len(rows) + dropped, total, HEADINGS[name])
            if dropped:
                self.assertEqual(named.pop(HEADINGS[name]), (dropped, total), notice)
            dropped_total += dropped
        self.assertEqual(named, {}, "the notice names a table that lost no rows")
        match = LEFT_OUT.search(notice)
        if match is None:
            self.fail(f"the notice gives no total: {notice}")
        self.assertEqual(int(match.group(1)), dropped_total, notice)
        return dropped_total


class TestTheCeiling(unittest.TestCase):
    def test_it_is_the_number_the_sync_side_trims_to(self) -> None:
        self.assertEqual(render_report.MAX_BODY_CHARS, 65_536)
        self.assertEqual(render_report.MAX_BODY_CHARS, sync_issue.BODY_LIMIT)

    def test_the_headings_the_notice_uses_are_the_ones_the_body_is_written_under(self) -> None:
        self.assertEqual(dict(render_report.TABLE_TITLES), HEADINGS)
        self.assertEqual(sync_issue.CURRENT_HEADER, f"## {render_report.TABLE_TITLES['current']}")


class TestAnOverLimitBody(Renderer):
    def test_accepted_rows_go_first_and_the_marker_keeps_the_full_open_set(self) -> None:
        code, body, summary, err = self.render(self.report(accepted=700))
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(body), render_report.MAX_BODY_CHARS)
        # A block is an eighth of what a table still shows, so the fit cannot overshoot by more.
        self.assertGreater(len(body), render_report.MAX_BODY_CHARS * 3 // 4)
        dropped = self.assert_notice_matches_the_tables(
            body, {"new": 1, "resolved": 0, "current": 1, "accepted": 700}
        )
        self.assertGreater(dropped, 0)
        self.assertEqual(table(body, "new")[1], 0)
        self.assertEqual(table(body, "current")[1], 0)
        self.assertEqual(table(body, "accepted")[1], dropped)
        self.assertIn("still carries the full open set, 1 finding, so the next run", body)
        self.assertNotIn("Change detection is partial", body)
        state = decode_marker(body)
        self.assertEqual([entry["fingerprint"] for entry in state], [CAPTURED])
        self.assertTrue(body.endswith(" -->\n"), "the marker is no longer last")
        self.assertEqual(
            (summary["open"], summary["accepted"], summary["omitted"]), (1, 700, 0), summary
        )

    def test_the_change_lists_outlast_the_full_list(self) -> None:
        code, body, summary, err = self.render(self.report(opened=600))
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(body), render_report.MAX_BODY_CHARS)
        dropped = self.assert_notice_matches_the_tables(
            body, {"new": 601, "resolved": 0, "current": 601, "accepted": 0}
        )
        new_rows, new_dropped = table(body, "new")
        current_rows, current_dropped = table(body, "current")
        self.assertEqual(current_rows, [], "Current gives up every row before New gives up one")
        self.assertEqual(current_dropped, 601)
        self.assertGreater(len(new_rows), 0)
        self.assertEqual(dropped, 601 + new_dropped)
        self.assertEqual(summary["open"], 601)

    def test_a_partial_marker_is_not_called_the_full_open_set(self) -> None:
        code, body, summary, err = self.render(self.report(opened=600))
        self.assertEqual(code, 0, err)
        self.assertGreater(summary["omitted"], 0)
        self.assertIn("**Change detection is partial.**", body)
        self.assertLess(body.index("**Change detection is partial.**"), body.index(NOTICE))
        self.assertNotIn("full open set", body)
        self.assertIn("leaving these rows out took nothing more from it", body)
        # The marker holds what it held before any row was dropped: the same entries the
        # unfitted render packs, not fewer.
        with mock.patch.object(render_report, "MAX_BODY_CHARS", 10**9):
            unfitted, _ = render_report.render(self.arguments(self.report(opened=600)))
        self.assertNotIn(NOTICE, unfitted)
        self.assertEqual(decode_marker(body), decode_marker(unfitted))
        self.assertEqual(len(decode_marker(body)), 601 - summary["omitted"])

    def test_rows_leave_from_the_low_severity_end_of_a_table(self) -> None:
        report = json.loads(self.report(opened=600).read_text(encoding="utf-8"))
        for entry in report["accounts"][1]["findings"][301:]:
            entry["severity"] = "warn"
        path = self.tmp / "mixed.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        code, body, summary, err = self.render(path)
        self.assertEqual(code, 0, err)
        self.assertEqual((summary["high"], summary["warn"]), (301, 300))
        rows, dropped = table(body, "new")
        self.assertGreater(dropped, 0)
        self.assertLessEqual(len(rows), 301)
        self.assertTrue(all(row.startswith("| high |") for row in rows))

    def test_a_resolved_table_is_cut_before_the_new_one(self) -> None:
        _, previous_body, _, _ = self.render(self.report(opened=40))
        previous = self.tmp / "previous.md"
        previous.write_text(previous_body, encoding="utf-8")
        before = {entry["fingerprint"] for entry in decode_marker(previous_body)}
        report = json.loads(self.report(opened=1200).read_text(encoding="utf-8"))
        findings = report["accounts"][1]["findings"]
        report["accounts"][1]["findings"] = [f for f in findings if f["fingerprint"] not in before]
        path = self.tmp / "moved.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        code, body, summary, err = self.render(path, previous)
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["resolved"], len(before))
        self.assertLessEqual(len(body), render_report.MAX_BODY_CHARS)
        totals = {
            "new": summary["new"],
            "resolved": summary["resolved"],
            "current": summary["open"],
            "accepted": 0,
        }
        self.assert_notice_matches_the_tables(body, totals)
        self.assertEqual(table(body, "current")[0], [])
        self.assertEqual(table(body, "resolved")[0], [])
        self.assertGreater(len(table(body, "new")[0]), 0)

    def test_nothing_a_report_chose_reaches_the_notice(self) -> None:
        report = json.loads(self.report(accepted=700).read_text(encoding="utf-8"))
        for entry in report["accounts"][1]["findings"]:
            entry["message"] = "[click](javascript:alert(1)) @all | <b>"
            entry["rule_id"] = "rule`|x"
        path = self.tmp / "hostile.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        code, body, _, err = self.render(path)
        self.assertEqual(code, 0, err)
        notice = next(line for line in body.splitlines() if line.startswith(NOTICE))
        self.assertRegex(notice, r"^[A-Za-z0-9 .,:*']+$")
        for line in body.splitlines():
            if DROPPED_ROW.match(line):
                self.assertRegex(line, r"^[a-z0-9 .|]+$")


class TestABodyThatFits(Renderer):
    def test_every_captured_report_renders_as_if_there_were_no_ceiling(self) -> None:
        for path in sorted(FIXTURES.glob("report-*.json")):
            with self.subTest(fixture=path.name):
                fitted, _ = render_report.render(self.arguments(path))
                with mock.patch.object(render_report, "MAX_BODY_CHARS", 10**9):
                    unfitted, _ = render_report.render(self.arguments(path))
                self.assertEqual(fitted, unfitted)
                self.assertNotIn(NOTICE, fitted)
                self.assertNotIn("left out to fit", fitted)
                self.assertLess(len(fitted), render_report.MAX_BODY_CHARS)

    def test_a_body_exactly_at_the_ceiling_is_left_alone(self) -> None:
        path = self.report(accepted=40)
        whole, _ = render_report.render(self.arguments(path))
        with mock.patch.object(render_report, "MAX_BODY_CHARS", len(whole)):
            at_the_ceiling, _ = render_report.render(self.arguments(path))
        self.assertEqual(at_the_ceiling, whole)
        with mock.patch.object(render_report, "MAX_BODY_CHARS", len(whole) - 1):
            one_over, _ = render_report.render(self.arguments(path))
        self.assertLess(len(one_over), len(whole))
        self.assertIn(NOTICE, one_over)

    def test_no_table_loses_a_row_without_the_notice(self) -> None:
        # Walk the ceiling down across a body of four tables and check the two always agree.
        _, previous_body, _, _ = self.render(self.report(opened=30))
        previous = self.tmp / "previous.md"
        previous.write_text(previous_body, encoding="utf-8")
        before = {entry["fingerprint"] for entry in decode_marker(previous_body)}
        report = json.loads(self.report(opened=60, accepted=30).read_text(encoding="utf-8"))
        findings = report["accounts"][1]["findings"]
        keep_one = sorted(before)[0]
        report["accounts"][1]["findings"] = [
            f for f in findings if f["fingerprint"] not in before or f["fingerprint"] == keep_one
        ]
        path = self.tmp / "four-tables.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        arguments = self.arguments(path, previous)
        whole, _ = render_report.render(arguments)
        totals = {"new": 30, "resolved": 30, "current": 31, "accepted": 30}
        for name, total in totals.items():
            self.assertEqual((len(table(whole, name)[0]), table(whole, name)[1]), (total, 0), name)
        seen_cut = 0
        for ceiling in range(len(whole), 2500, -997):
            with self.subTest(ceiling=ceiling):
                with mock.patch.object(render_report, "MAX_BODY_CHARS", ceiling):
                    body, _ = render_report.render(arguments)
                if NOTICE in body:
                    seen_cut += 1
                    self.assertLessEqual(len(body), ceiling)
                    self.assert_notice_matches_the_tables(body, totals)
                else:
                    self.assertEqual(body, whole)
                    for name in totals:
                        self.assertEqual(table(body, name)[1], 0)
                self.assertEqual(decode_marker(body), decode_marker(whole))
        self.assertGreater(seen_cut, 3)


class TestABodyRowsCannotSave(Renderer):
    def test_it_is_left_whole_for_the_sync_side_to_refuse(self) -> None:
        # The account table is not a finding table. When it alone is over the ceiling, cutting
        # findings would lose rows and still post nothing, so the body comes back as before
        # this task: every row, no notice, and sync_issue.py's own refusal still to come.
        code, body, summary, err = self.render(self.report(opened=5, accounts=900))
        self.assertEqual(code, 0, err)
        self.assertGreater(len(body), render_report.MAX_BODY_CHARS)
        self.assertNotIn(NOTICE, body)
        for name, total in (("new", 6), ("current", 6)):
            rows, dropped = table(body, name)
            self.assertEqual((len(rows), dropped), (total, 0))
        self.assertEqual(len(decode_marker(body)), 6)
        self.assertEqual(summary["open"], 6)


if __name__ == "__main__":
    unittest.main()
