"""Tests for folders against a fake hearings root built per test."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exceptions import SystemProblem  # noqa: E402
from folders import SUBFOLDERS, prepare_day_folder  # noqa: E402

DAY = date(2026, 1, 5)


def test_creates_day_folder_with_four_subfolders(tmp_path: Path) -> None:
    """The day folder holds exactly the four subfolders, and CCIS comes back."""
    ccis = prepare_day_folder(tmp_path, DAY)

    day_folder = tmp_path / "01.05.2026"
    assert ccis == day_folder / "CCIS"
    assert sorted(p.name for p in day_folder.iterdir()) == sorted(SUBFOLDERS)
    assert all((day_folder / name).is_dir() for name in SUBFOLDERS)


def test_date_folder_pads_month_and_day(tmp_path: Path) -> None:
    """A two-digit month and day are kept as they are."""
    prepare_day_folder(tmp_path, date(2026, 10, 20))

    assert (tmp_path / "10.20.2026").is_dir()


def test_second_run_same_day_reuses_folder(tmp_path: Path) -> None:
    """A rerun keeps files already in the day folder."""
    ccis = prepare_day_folder(tmp_path, DAY)
    (ccis / "DOE_JOHN.pdf").write_bytes(b"%PDF")
    (ccis.parent / "Affidavit" / "note.txt").write_text("kept")

    assert prepare_day_folder(tmp_path, DAY) == ccis
    assert (ccis / "DOE_JOHN.pdf").read_bytes() == b"%PDF"
    assert (ccis.parent / "Affidavit" / "note.txt").read_text() == "kept"


def test_other_days_are_left_alone(tmp_path: Path) -> None:
    """Making today's folder does not touch an earlier day's folder."""
    earlier = tmp_path / "01.04.2026"
    earlier.mkdir()

    prepare_day_folder(tmp_path, DAY)

    assert list(earlier.iterdir()) == []


def test_missing_root_stops_and_creates_nothing(tmp_path: Path) -> None:
    """A root that is not there is never created, so a dropped drive is caught."""
    root = tmp_path / "missing"

    with pytest.raises(SystemProblem, match="cannot be reached"):
        prepare_day_folder(root, DAY)
    assert not root.exists()


def test_root_that_is_a_file_stops(tmp_path: Path) -> None:
    """A file where the root folder should be is refused."""
    root = tmp_path / "root"
    root.write_text("")

    with pytest.raises(SystemProblem, match="cannot be reached"):
        prepare_day_folder(root, DAY)


def test_folder_that_cannot_be_made_stops(tmp_path: Path) -> None:
    """A file named like the day folder blocks it, and the run stops."""
    (tmp_path / "01.05.2026").write_text("")

    with pytest.raises(SystemProblem, match="Could not create"):
        prepare_day_folder(tmp_path, DAY)


def test_error_messages_leave_out_the_path(tmp_path: Path) -> None:
    """Messages reach the console and logs, so the network path stays out."""
    root = tmp_path / "hearings-root-name"

    with pytest.raises(SystemProblem) as caught:
        prepare_day_folder(root, DAY)
    assert "hearings-root-name" not in str(caught.value)
