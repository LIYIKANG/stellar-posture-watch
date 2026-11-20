#!/usr/bin/env python3
"""A fixture that is listed must also be used, and a fixture that is used must be listed (9.10).

`TestFixtureProvenance` in `tests/test_render_report.py` compares the provenance table with the
files on disk, and `tests/test_fixture_manifest.py` binds each row to its bytes. Neither asks
whether a test ever opens the file, so a capture could sit in `tests/fixtures/` with a row that
says what it exercises while nothing reads it.

This module closes that in both directions by reading the test modules as text. It imports none
of them, runs none of them and measures nothing at run time: a fixture counts as reachable when
a test module spells its file name.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
README_NAME = "README.md"

# A report name as a test module spells it. The glob `report-*.json` is not one: the star is
# outside the class, so a directory sweep cannot stand in for naming a file.
REPORT_NAME = re.compile(r"report-[A-Za-z0-9_-]+\.json")
TABLE_ROW = re.compile(r"^\| `(report-[^`]+\.json)`", re.MULTILINE)

# Modules whose subject is the provenance table itself. They spell fixture names to check rows
# and write throwaway reports into temporary copies, so a name there is neither a use of a
# capture nor a claim that one exists.
TABLE_CHECKS = frozenset({"test_fixture_manifest.py", "test_fixture_reachability.py"})


def fixture_files(tests_dir: Path) -> set[str]:
    """Every file in the fixtures directory except the table that describes them."""
    fixtures = tests_dir / "fixtures"
    return {path.name for path in fixtures.iterdir() if path.is_file() and path.name != README_NAME}


def table_names(tests_dir: Path) -> set[str]:
    readme = (tests_dir / "fixtures" / README_NAME).read_text(encoding="utf-8")
    return set(TABLE_ROW.findall(readme))


def module_texts(tests_dir: Path) -> dict[str, str]:
    """The source of each test module that exercises fixtures, read as text and never imported."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(tests_dir.glob("test_*.py"))
        if path.name not in TABLE_CHECKS
    }


def unreachable(tests_dir: Path) -> list[str]:
    """Fixture files that no test module names."""
    sources = module_texts(tests_dir)
    return sorted(
        name
        for name in fixture_files(tests_dir)
        if not any(name in text for text in sources.values())
    )


def unlisted(tests_dir: Path) -> dict[str, list[str]]:
    """Report names a test module spells that have no provenance row, with who spells them."""
    listed = table_names(tests_dir)
    found: dict[str, list[str]] = {}
    for module, text in module_texts(tests_dir).items():
        for name in sorted(set(REPORT_NAME.findall(text)) - listed):
            found.setdefault(name, []).append(module)
    return dict(sorted(found.items()))


def unreachable_message(names: list[str]) -> str:
    lines = [f"tests/fixtures/{name} is not named by any test module" for name in names]
    lines.append(
        "A fixture nobody opens proves nothing. Read it from a test, or remove the file and its "
        "row in tests/fixtures/README.md."
    )
    return "\n".join(lines)


def unlisted_message(found: dict[str, list[str]]) -> str:
    lines = [
        f"{name} is named in tests/{', tests/'.join(modules)} but has no row in "
        "tests/fixtures/README.md"
        for name, modules in found.items()
    ]
    lines.append(
        "A fixture's provenance is written down, not inferred. Add the row, or give a temporary "
        "file a name that does not look like a report fixture."
    )
    return "\n".join(lines)


class TestThisRepository(unittest.TestCase):
    def test_every_fixture_file_is_named_by_a_test_module(self) -> None:
        missing = unreachable(TESTS)
        self.assertEqual(missing, [], unreachable_message(missing))

    def test_every_report_named_by_a_test_module_has_a_provenance_row(self) -> None:
        found = unlisted(TESTS)
        self.assertEqual(found, {}, unlisted_message(found))

    def test_the_scan_is_reading_real_modules_and_real_rows(self) -> None:
        # Two empty sets would agree with each other, so the scan has to show it saw something.
        sources = module_texts(TESTS)
        self.assertIn("test_render_report.py", sources)
        self.assertTrue(TABLE_CHECKS.isdisjoint(sources))
        named = {name for text in sources.values() for name in REPORT_NAME.findall(text)}
        self.assertGreaterEqual(len(named), 6)
        self.assertGreaterEqual(len(table_names(TESTS)), 6)
        self.assertGreaterEqual(len(fixture_files(TESTS)), 7)


class TestTheCheckItself(unittest.TestCase):
    """Runs the same functions over a small tree of its own, so the failure can be seen.

    The tree is written here rather than copied from this repository, so a fixture somebody
    adds next to this file moves the two tests above and none of these.
    """

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.tests_dir = Path(self._directory.name) / "tests"
        fixtures = self.tests_dir / "fixtures"
        fixtures.mkdir(parents=True)
        for name in ("report-one.json", "report-two.json", "policy-probe.toml"):
            (fixtures / name).write_text("{}\n", encoding="utf-8")
        (fixtures / README_NAME).write_text(
            "# Report fixtures\n\n"
            "| File | Origin | sha256 | What it exercises |\n"
            "| --- | --- | --- | --- |\n"
            f"| `report-one.json` | real run | `{'1' * 64}` | one path |\n"
            f"| `report-two.json` | derived from `report-one.json` | `{'2' * 64}` | another |\n",
            encoding="utf-8",
        )
        self.add_module(
            "test_render.py",
            'ONE = "report-one.json"\nTWO = "report-two.json"\nPROBE = "policy-probe.toml"\n',
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def add_fixture(self, name: str, *, row: bool) -> None:
        (self.tests_dir / "fixtures" / name).write_text("{}\n", encoding="utf-8")
        if row:
            readme = self.tests_dir / "fixtures" / README_NAME
            with readme.open("a", encoding="utf-8") as handle:
                handle.write(f"| `{name}` | throwaway | `{'0' * 64}` | nothing |\n")

    def add_module(self, name: str, source: str) -> None:
        (self.tests_dir / name).write_text(source, encoding="utf-8")

    def test_the_copy_starts_clean(self) -> None:
        self.assertEqual(unreachable(self.tests_dir), [])
        self.assertEqual(unlisted(self.tests_dir), {})

    def test_a_listed_fixture_nobody_opens_is_named_and_passes_once_removed(self) -> None:
        self.add_fixture("report-unused.json", row=True)
        self.assertIn("report-unused.json", table_names(self.tests_dir))
        missing = unreachable(self.tests_dir)
        self.assertEqual(missing, ["report-unused.json"])
        self.assertIn(
            "tests/fixtures/report-unused.json is not named by any test module",
            unreachable_message(missing),
        )
        # The row is there, so the other direction has nothing to say about it.
        self.assertEqual(unlisted(self.tests_dir), {})
        (self.tests_dir / "fixtures" / "report-unused.json").unlink()
        self.assertEqual(unreachable(self.tests_dir), [])

    def test_a_test_that_names_the_fixture_makes_it_reachable(self) -> None:
        self.add_fixture("report-unused.json", row=True)
        self.add_module("test_new.py", 'NAME = "report-unused.json"\n')
        self.assertEqual(unreachable(self.tests_dir), [])
        self.assertEqual(unlisted(self.tests_dir), {})

    def test_a_directory_sweep_does_not_count_as_naming_a_fixture(self) -> None:
        self.add_fixture("report-unused.json", row=True)
        self.add_module("test_new.py", 'PATHS = sorted(FIXTURES.glob("report-*.json"))\n')
        self.assertEqual(unreachable(self.tests_dir), ["report-unused.json"])

    def test_a_module_that_only_checks_the_table_does_not_make_a_fixture_reachable(self) -> None:
        self.add_fixture("report-unused.json", row=True)
        self.add_module("test_fixture_manifest.py", 'NAME = "report-unused.json"\n')
        self.assertEqual(unreachable(self.tests_dir), ["report-unused.json"])

    def test_a_file_that_is_not_a_report_has_to_be_named_too(self) -> None:
        self.add_fixture("policy-nobody-reads.toml", row=False)
        self.assertEqual(unreachable(self.tests_dir), ["policy-nobody-reads.toml"])

    def test_a_report_named_by_a_test_with_no_row_is_reported_with_its_module(self) -> None:
        self.add_module("test_new.py", 'NAME = "report-typo.json"\n')
        self.add_module("test_other.py", 'NAME = "report-typo.json"  # and "report-one.json"\n')
        found = unlisted(self.tests_dir)
        self.assertEqual(found, {"report-typo.json": ["test_new.py", "test_other.py"]})
        message = unlisted_message(found)
        self.assertIn(
            "report-typo.json is named in tests/test_new.py, tests/test_other.py", message
        )
        self.assertIn("has no row in tests/fixtures/README.md", message)

    def test_a_fixture_on_disk_with_neither_a_row_nor_a_reader_fails_the_first_way(self) -> None:
        self.add_fixture("report-orphan.json", row=False)
        self.assertEqual(unreachable(self.tests_dir), ["report-orphan.json"])
        self.assertEqual(unlisted(self.tests_dir), {})

    def test_the_scan_reads_text_and_never_imports_a_module(self) -> None:
        # Importing this would raise; reading it does not, and its name still counts.
        self.add_fixture("report-unused.json", row=True)
        self.add_module(
            "test_broken.py",
            'raise SystemExit("imported")\nthis is not python "report-unused.json"\n',
        )
        self.assertEqual(unreachable(self.tests_dir), [])


if __name__ == "__main__":
    unittest.main()
