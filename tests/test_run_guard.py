"""Tests for the one-run-at-a-time lock, the done marker, and how main uses them.

main's run is driven with fake email, sheet and CCIS steps, so nothing here
reaches Graph, CCIS or a real drive.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from excel_reader import Person  # noqa: E402
from exceptions import SystemProblem  # noqa: E402
from folders import day_folder  # noqa: E402
from run_guard import (  # noqa: E402
    DONE_MARKER,
    AlreadyRunning,
    finished_today,
    mark_finished,
    single_run,
)

TODAY = date(2026, 1, 5)
PEOPLE = [
    Person(4, "DOE, JOHN", "DOE", "JOHN", "01/01/1900", None),
    Person(9, "ROE, JANE", "ROE", "JANE", "01/01/1950", None),
    Person(14, "POE", "", "", "01/01/1900", "name has no comma to separate last from first"),
]


# The lock


def test_second_copy_is_refused_while_the_first_runs(tmp_path: Path) -> None:
    """A second open of the lock while it is held fails."""
    with single_run(tmp_path):
        with pytest.raises(AlreadyRunning, match="already running"):
            with single_run(tmp_path):
                pass


def test_lock_is_free_again_after_the_run(tmp_path: Path) -> None:
    """Once a run ends, the next one gets the lock."""
    with single_run(tmp_path):
        pass
    with single_run(tmp_path):
        pass


def test_lock_is_released_when_the_run_fails(tmp_path: Path) -> None:
    """A run that stops on an error does not leave the lock held."""
    with pytest.raises(SystemProblem):
        with single_run(tmp_path):
            raise SystemProblem("stopped")
    with single_run(tmp_path):
        pass


# The marker


def test_marker_round_trip(tmp_path: Path) -> None:
    """No marker means not done; writing it means done."""
    assert not finished_today(tmp_path)

    mark_finished(tmp_path, "Searched 2.", datetime(2026, 1, 5, 6, 12))

    assert finished_today(tmp_path)
    text = (tmp_path / DONE_MARKER).read_text(encoding="utf-8")
    assert "01/05/2026 06:12" in text
    assert "Searched 2." in text
    assert "--force" in text


def test_marker_that_cannot_be_written_is_reported(tmp_path: Path) -> None:
    """A day folder that is gone fails loudly rather than silently not marking."""
    with pytest.raises(SystemProblem, match="search everyone again"):
        mark_finished(tmp_path / "missing", "Searched 2.", datetime(2026, 1, 5, 6, 12))


# main's run with the guards


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake config and fake steps; records how many times CCIS was searched."""
    root = tmp_path / "hearings"
    root.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config = {"paths": {"hearings_root": root, "logs": logs}}
    state = {"config": config, "root": root, "searches": 0, "fetches": 0}

    sheet = tmp_path / "sheet.xlsx"
    sheet.write_bytes(b"fake xlsx bytes")
    state["sheet"] = sheet

    def fetch(config: dict, today: date) -> tuple[Path, str]:
        state["fetches"] += 1
        return sheet, "Copy of Bookings List.xlsx"

    def search(config: dict, people: list, pdf_dir: Path) -> dict:
        state["searches"] += 1
        return {
            main.HISTORY: [p.row for p in people],
            main.NO_RESULTS: [],
            main.NEEDS_A_LOOK: [],
        }

    monkeypatch.setattr(main, "fetch_from_email", fetch)
    monkeypatch.setattr(main, "read_people", lambda path: list(PEOPLE))
    monkeypatch.setattr(main, "search_people", search)
    return state


def _run(state: dict, limit: int | None = None, force: bool = False, path: Path | None = None) -> int:
    """Run main's guarded body for TODAY."""
    return main._guarded_run(state["config"], TODAY, path, limit, force)


def _marker(state: dict) -> Path:
    """Where today's marker goes."""
    return day_folder(state["root"], TODAY) / DONE_MARKER


def test_full_email_run_marks_the_day(setup: dict) -> None:
    """A full run writes the marker with counts only."""
    assert _run(setup) == 0

    text = _marker(setup).read_text(encoding="utf-8")
    assert "Searched 2: criminal history 2, no results found 0, needs a look by hand 0." in text
    assert "Flagged, not searched: 1." in text
    for name in ("DOE", "ROE", "POE", "JOHN", "JANE"):
        assert name not in text


def test_second_run_the_same_day_stops_before_the_email(setup: dict) -> None:
    """Once marked, a rerun neither fetches nor searches."""
    _run(setup)
    assert _run(setup) == 0

    assert setup["fetches"] == 1
    assert setup["searches"] == 1


def test_force_runs_a_marked_day_again(setup: dict) -> None:
    """--force searches again and leaves the day marked."""
    _run(setup)
    _run(setup, force=True)

    assert setup["searches"] == 2
    assert _marker(setup).is_file()


def test_limit_run_does_not_mark_the_day(setup: dict) -> None:
    """A trial run with --limit leaves the day open for the real run."""
    _run(setup, limit=1)

    assert not _marker(setup).exists()
    _run(setup)
    assert setup["searches"] == 2


def test_local_file_run_ignores_and_never_writes_the_marker(setup: dict, tmp_path: Path) -> None:
    """A local sheet may not be today's, so it neither checks nor marks."""
    _run(setup, path=tmp_path / "local.xlsx")
    assert not _marker(setup).exists()

    _run(setup)
    _run(setup, path=tmp_path / "local.xlsx")
    assert setup["searches"] == 3


def test_failed_search_does_not_mark_the_day(setup: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that stops partway leaves the day open."""
    def broken(config: dict, people: list, pdf_dir: Path) -> dict:
        raise SystemProblem("CCIS went away")

    monkeypatch.setattr(main, "search_people", broken)

    with pytest.raises(SystemProblem):
        _run(setup)
    assert not _marker(setup).exists()


def test_missing_drive_stops_before_the_email(setup: dict) -> None:
    """An unreachable root is caught before any waiting."""
    setup["root"].rmdir()

    with pytest.raises(SystemProblem, match="cannot be reached"):
        _run(setup)
    assert setup["fetches"] == 0


# The sheet on the drive


def _sheet_copy(state: dict) -> Path:
    """Where the emailed sheet is copied, under its name from the email."""
    return day_folder(state["root"], TODAY) / "Copy of Bookings List.xlsx"


def test_email_run_copies_the_sheet_beside_the_four_folders(setup: dict) -> None:
    """The day folder gets the sheet under the name it had on the email."""
    _run(setup)

    assert _sheet_copy(setup).read_bytes() == b"fake xlsx bytes"


def test_sheet_is_copied_before_the_search(setup: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that stops in CCIS still leaves the sheet on the drive."""
    def broken(config: dict, people: list, pdf_dir: Path) -> dict:
        raise SystemProblem("CCIS went away")

    monkeypatch.setattr(main, "search_people", broken)

    with pytest.raises(SystemProblem):
        _run(setup)
    assert _sheet_copy(setup).exists()


def test_local_file_run_does_not_copy_a_sheet(setup: dict, tmp_path: Path) -> None:
    """A local sheet may not be today's, so it stays off the drive."""
    _run(setup, path=tmp_path / "local.xlsx")

    assert not _sheet_copy(setup).exists()


def test_failed_copy_is_logged_and_the_run_goes_on(setup: dict, caplog: pytest.LogCaptureFixture) -> None:
    """The PDFs matter more than the copy, and the sheet is still in logs."""
    setup["sheet"].unlink()

    assert _run(setup) == 0
    assert setup["searches"] == 1
    assert _marker(setup).exists()
    assert "Could not copy the spreadsheet to the day folder: FileNotFoundError" in caplog.text
