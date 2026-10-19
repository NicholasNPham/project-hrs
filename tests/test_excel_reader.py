"""Tests for excel_reader against a fake booking workbook built per test."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from excel_reader import (  # noqa: E402
    Columns,
    SpreadsheetLayoutError,
    check_dob,
    extract_dob,
    find_columns,
    parse_name,
    read_people,
)

HEADERS = [
    "Booking#",
    "Name\nCharge Description",
    "Housing Facility\nCase Number",
    "Race/Sex DOB\nType of Bail",
    "Jail ID\nBail Amount",
    "Arst\nAgency",
]


def _write_workbook(
    path: Path, rows: list[list[object]], extra_sheet: list[list[object]] | None = None
) -> Path:
    """Save rows to a workbook, optionally with a second sheet, and return its path."""
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    if extra_sheet is not None:
        second = workbook.create_sheet("Sheet2")
        for row in extra_sheet:
            second.append(row)
    workbook.save(path)
    return path


@pytest.fixture
def booking_sheet(tmp_path: Path) -> Path:
    """A workbook laid out like the real export, with fake people."""
    return _write_workbook(
        tmp_path / "bookings.xlsx",
        [
            ["Daily Booking Report by Name"],  # 1: title, Name but no DOB
            [None],  # 2
            HEADERS,  # 3
            ["B1", "DOE, JOHN A", "MAIN", "W/M 01/01/1900", "J1", "SO"],  # 4
            [None, "BATTERY", "CASE-1", "BOND", "500.00", None],  # 5
            [None, "VOP WARRANT 01/02/2026", "CASE-2", "NO BOND", None, None],  # 6
            ["B2", "ROE JR, RICHARD", "MAIN", "B M 2/2/1900", "J2", "PD"],  # 7
            [None, "TRESPASS", "CASE-3", "ROR", None, None],  # 8
            ["B3", "SMITH JANE", "MAIN", "W/F 03/03/1900", "J3", "SO"],  # 9
            ["B4", "POE, EDGAR", "MAIN", datetime(1950, 4, 4), "J4", "SO"],  # 10
            ["B5", "DOE, JANE", "MAIN", "W/F 13/45/1900", "J5", "SO"],  # 11
            ["B6", None, "MAIN", "W/F 05/05/1900", "J6", "SO"],  # 12
            ["B7", "STATUTE, X", "MAIN", "W/M", "J7", "SO"],  # 13: detail, comma
            ["000.00", "DOE, JAY", "MAIN", "W/M 6/6/1900", "J8", "SO"],  # 14
            ["B9", "DOE, JOE", "MAIN", "W/M 7-7-1900", "J9", "SO"],  # 15: dashes
            ["B10", "DOE, JON", "MAIN", "W/M 8/8/85", "J10", "SO"],  # 16: 2-digit year
            ["B11", "DOE, JOHN JOHN, JR", "MAIN", "B M 9/9/1900", "J11", "SO"],  # 17
        ],
    )


def test_read_people_keeps_only_main_rows(booking_sheet: Path) -> None:
    """Rows without a date in the DOB column are skipped, whatever else they hold."""
    rows = [person.row for person in read_people(booking_sheet)]
    assert rows == [4, 7, 9, 10, 11, 12, 14, 15, 16, 17]


def test_read_people_values(booking_sheet: Path) -> None:
    """Each main row comes back with the right search name, DOB and problem."""
    people = {person.row: person for person in read_people(booking_sheet)}

    assert (people[4].last, people[4].first, people[4].dob) == ("DOE", "JOHN", "01/01/1900")
    assert people[4].problem is None
    assert (people[7].last, people[7].first, people[7].dob) == ("ROE", "RICHARD", "02/02/1900")
    assert people[7].problem is None
    assert people[10].dob == "04/04/1950"
    assert people[10].problem is None
    assert people[15].dob == "07/07/1900"
    assert people[15].problem is None
    assert (people[17].last, people[17].first) == ("DOE", "JOHN")
    assert people[17].problem is None

    assert people[9].problem == "name has no comma to separate last from first"
    assert people[11].problem == "DOB is not a real date"
    assert people[12].problem == "name is blank"
    assert people[14].problem is None
    assert people[16].problem == "DOB has a two-digit year"


def test_read_people_reports_every_problem(tmp_path: Path) -> None:
    """A row with several problems lists all of them."""
    path = _write_workbook(
        tmp_path / "multi.xlsx",
        [HEADERS, [None, "SMITH JANE", "MAIN", "W/F 13/45/1900", "J1", "SO"]],
    )
    [person] = read_people(path)
    assert person.problem == (
        "name has no comma to separate last from first; DOB is not a real date"
    )


def test_read_people_missing_header(tmp_path: Path) -> None:
    """A sheet without the expected headers stops before any search."""
    path = _write_workbook(tmp_path / "bad.xlsx", [["Booking#", "Inmate"]])
    with pytest.raises(SpreadsheetLayoutError):
        read_people(path)


def test_read_people_rejects_second_sheet_with_data(tmp_path: Path) -> None:
    """People on a second sheet would be missed, so the run stops."""
    path = _write_workbook(
        tmp_path / "two.xlsx", [HEADERS], extra_sheet=[["B1", "DOE, JOHN"]]
    )
    with pytest.raises(SpreadsheetLayoutError):
        read_people(path)


def test_read_people_allows_empty_second_sheet(tmp_path: Path) -> None:
    """An empty leftover sheet does not block the run."""
    path = _write_workbook(
        tmp_path / "two.xlsx",
        [HEADERS, ["B1", "DOE, JOHN", "MAIN", "W/M 1/1/1900", "J1", "SO"]],
        extra_sheet=[],
    )
    assert len(read_people(path)) == 1


def test_find_columns_skips_title_row() -> None:
    """A title row with only Name in it is not mistaken for the header."""
    rows = [("Report by Name",), tuple(HEADERS)]
    assert find_columns(rows) == Columns(header_row=2, name=1, dob=3)


def test_find_columns_rejects_two_name_columns() -> None:
    """Two Name columns are ambiguous, so the run stops."""
    rows = [("Booking#", "Name", "Name", "Race/Sex DOB")]
    with pytest.raises(SpreadsheetLayoutError):
        find_columns(rows)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("W/M 01/01/1900", "01/01/1900"),
        ("W/M\n01/01/1900", "01/01/1900"),
        ("B M 3/6/1900", "03/06/1900"),
        ("W/M 1/12/1900", "01/12/1900"),
        ("W/M 1-12-1900", "01/12/1900"),
        ("W/M 1.12.1900", "01/12/1900"),
        ("W/M 1/1/85", "01/01/85"),
        (datetime(1950, 4, 4), "04/04/1950"),
        ("BOND", None),
        ("W/M", None),
        (None, None),
    ],
)
def test_extract_dob(value: object, expected: str | None) -> None:
    """Any m/d/y date in the cell is found and padded."""
    assert extract_dob(value) == expected


@pytest.mark.parametrize(
    ("dob", "expected"),
    [
        ("01/01/1900", None),
        ("02/29/2000", None),
        ("02/29/1900", "DOB is not a real date"),
        ("13/01/1900", "DOB is not a real date"),
        ("01/01/85", "DOB has a two-digit year"),
        ("01/01/1899", "DOB is outside the believable range"),
        (
            (date.today() + timedelta(days=1)).strftime("%m/%d/%Y"),
            "DOB is outside the believable range",
        ),
    ],
)
def test_check_dob(dob: str, expected: str | None) -> None:
    """Only a real four-digit-year date in a believable range is searchable."""
    assert check_dob(dob) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DOE, JOHN", ("DOE", "JOHN", None)),
        ("DOE,JOHN", ("DOE", "JOHN", None)),
        ("DOE, JOHN ALLEN", ("DOE", "JOHN", None)),
        ("  DOE ,  JOHN  ", ("DOE", "JOHN", None)),
        ("DOE\u00a0JR,\u200bJOHN", ("DOE", "JOHN", None)),
        ("DOE JR, JOHN", ("DOE", "JOHN", None)),
        ("DOE, JOHN III", ("DOE", "JOHN", None)),
        ("DOE, JOHN JR.", ("DOE", "JOHN", None)),
        ("DOE, JOHN JOHN, JR", ("DOE", "JOHN", None)),
        ("DOE, JOHN, III", ("DOE", "JOHN", None)),
        ("DOE, JOHN, I", ("DOE", "JOHN", None)),
        ("DOE, JOHN, IV.", ("DOE", "JOHN", None)),
        ("DOE, JOHN, 2ND", ("DOE", "JOHN", None)),
        ("DOE, JR, JOHN", ("DOE", "JOHN", None)),
        ("DOE, JOHN, ALLEN", ("DOE", "JOHN", None)),
        ("DOE, J.", ("DOE", "J", None)),
        ('DOE, "JJ" JOHN', ("DOE", "JOHN", None)),
        ("DOE, JOHN (AKA ROE, JIM)", ("DOE", "JOHN", None)),
        ("DE LA CRUZ, JUAN", ("DE LA CRUZ", "JUAN", None)),
        ("SMITH-JONES, MARY", ("SMITH-JONES", "MARY", None)),
        ("JR, JOHN", ("JR", "JOHN", None)),
        ("", ("", "", "name is blank")),
        ('"JJ"', ("", "", "name is blank")),
        ("DOE JOHN", ("", "", "name has no comma to separate last from first")),
        ("DOE,", ("", "", "name is missing a last or first name")),
        ("DOE, JR", ("", "", "name is missing a last or first name")),
        (", JOHN", ("", "", "name is missing a last or first name")),
    ],
)
def test_parse_name(raw: str, expected: tuple[str, str, str | None]) -> None:
    """Names split into search surname and first name, or get flagged."""
    assert parse_name(raw) == expected
