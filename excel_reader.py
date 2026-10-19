"""Read the people to search from the jail booking spreadsheet.

Each person has one main row with their name and Race/Sex DOB, followed by
any number of detail rows holding charges, case numbers and bail. Detail rows
put other text in every column, Booking# included (the first real export had
text there on 345 of 346 rows), so the only marker of a person is a date in
the DOB cell. A main row whose name or DOB cannot be searched is flagged, not
skipped, so no person drops out of the run without a reason on record.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from openpyxl import Workbook, load_workbook

# The header sits near the top, below any title rows the export adds.
HEADER_SCAN_ROWS = 20

# The first real export wrote dates unpadded (m/d/yyyy). Dashes, dots and
# two-digit years are matched too, so a differently formatted DOB marks the
# row as a person and gets flagged instead of hiding it.
DOB_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d)")
NAME_HEADER = re.compile(r"\bNAME\b")
DOB_HEADER = re.compile(r"\bDOB\b")

# Dropped from the search name only. CCIS stores the bare surname, so
# searching "DOE JR" would come back "no results found" for a real record.
SUFFIXES = {
    "JR", "SR", "I", "II", "III", "IV", "V", "VI", "2ND", "3RD", "4TH",
}

# Nicknames and aliases in quotes or parentheses are not part of the legal
# name CCIS searches on.
ASIDE_PATTERN = re.compile(r'\([^)]*\)|"[^"]*"')

# Characters that survive a copy from a web page but are not whitespace to
# str.split, so they would end up inside a search name.
INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), " ")

EARLIEST_BIRTH_YEAR = 1900


class SpreadsheetLayoutError(Exception):
    """The spreadsheet does not have the expected layout."""


class Columns(NamedTuple):
    """Where the header row is and which 0-based columns matter."""

    header_row: int
    name: int
    dob: int


class Person(NamedTuple):
    """One main row from the spreadsheet, ready for a CCIS search."""

    row: int
    raw_name: str
    last: str
    first: str
    dob: str
    problem: str | None


def _clean_text(value: object) -> str:
    """Turn a cell value into one line of text with single spaces."""
    if value is None:
        return ""
    return " ".join(str(value).translate(INVISIBLE_CHARS).split())


def _clean_header(value: object) -> str:
    """Collapse a two-line header cell into one uppercase line."""
    return _clean_text(_clean_text(value).replace('"', " ")).upper()


def _cell(values: tuple[object, ...], index: int) -> object:
    """Return the value at index, or None past the end of a short row."""
    return values[index] if index < len(values) else None


def find_columns(rows: Iterable[tuple[object, ...]]) -> Columns:
    """Find the header row and the Name and DOB columns.

    The header is the first row with both a Name and a DOB cell, so a title
    such as "Bookings by Name" is skipped. Raises SpreadsheetLayoutError if
    that row has more than one of either, so a changed export stops the run
    before any CCIS search.
    """
    for row_number, values in enumerate(rows, start=1):
        headers = [_clean_header(value) for value in values]
        name_cols = [i for i, h in enumerate(headers) if NAME_HEADER.search(h)]
        dob_cols = [i for i, h in enumerate(headers) if DOB_HEADER.search(h)]
        if not name_cols or not dob_cols:
            continue
        if len(name_cols) == len(dob_cols) == 1:
            return Columns(row_number, name_cols[0], dob_cols[0])
        raise SpreadsheetLayoutError(
            f"Header row {row_number} has {len(name_cols)} Name and "
            f"{len(dob_cols)} DOB columns, expected one of each."
        )
    raise SpreadsheetLayoutError(
        f"No Name and DOB header found in the first {HEADER_SCAN_ROWS} rows."
    )


def extract_dob(value: object) -> str | None:
    """Return the date in a Race/Sex DOB cell as mm/dd/yyyy, or None.

    None means no date at all. A two-digit year comes back as mm/dd/yy so
    check_dob can flag it; guessing the century could search the wrong
    person. A date stored as a real Excel date is accepted too.
    """
    if isinstance(value, (datetime, date)):
        return value.strftime("%m/%d/%Y")
    match = DOB_PATTERN.search(_clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    return f"{int(month):02d}/{int(day):02d}/{year}"


def check_dob(dob: str) -> str | None:
    """Return why a DOB cannot be searched, or None if it can."""
    if len(dob) != 10:
        return "DOB has a two-digit year"
    try:
        born = datetime.strptime(dob, "%m/%d/%Y").date()
    except ValueError:
        return "DOB is not a real date"
    if born.year < EARLIEST_BIRTH_YEAR or born > date.today():
        return "DOB is outside the believable range"
    return None


def _is_suffix(token: str) -> bool:
    """Check whether a name token is a generational suffix such as JR or III."""
    return token.rstrip(".").upper() in SUFFIXES


def _drop_suffix(tokens: list[str]) -> list[str]:
    """Remove a trailing generational suffix, keeping at least one token."""
    if len(tokens) > 1 and _is_suffix(tokens[-1]):
        return tokens[:-1]
    return tokens


def parse_name(raw: str) -> tuple[str, str, str | None]:
    """Split "LAST, FIRST MIDDLE" into the surname and first name to search.

    Returns (last, first, problem). The surname is everything before the
    first comma. The first name is the first word after it that is not a
    suffix, so "LAST, FIRST, JR" and "LAST, FIRST MIDDLE, III" both work and
    anything after a later comma cannot change the search. The middle name
    is dropped because a middle name CCIS does not have would hide a real
    record. A name with no comma is flagged rather than guessed at, since a
    wrong split gives a false "no results found".
    """
    text = _clean_text(ASIDE_PATTERN.sub(" ", raw))
    if not text:
        return "", "", "name is blank"
    if "," not in text:
        return "", "", "name has no comma to separate last from first"

    last_part, first_part = text.split(",", 1)
    last_tokens = _drop_suffix(last_part.split())
    first_tokens = [
        token.strip(".")
        for token in first_part.replace(",", " ").split()
        if not _is_suffix(token)
    ]
    first_tokens = [token for token in first_tokens if token]
    if not last_tokens or not first_tokens:
        return "", "", "name is missing a last or first name"
    return " ".join(last_tokens), first_tokens[0], None


def _other_sheets_with_data(workbook: Workbook) -> list[str]:
    """Name every sheet after the first that has any non-empty cell."""
    return [
        sheet.title
        for sheet in workbook.worksheets[1:]
        if any(
            _clean_text(value)
            for row in sheet.iter_rows(values_only=True)
            for value in row
        )
    ]


def read_people(path: Path) -> list[Person]:
    """Read every main row from the first sheet, in sheet order.

    A main row with a bad name or DOB is returned with every reason in
    problem instead of being dropped, so it can be sent for manual review.
    Raises SpreadsheetLayoutError if another sheet holds data, because those
    people would otherwise be missed without a trace.
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        extra = _other_sheets_with_data(workbook)
        if extra:
            raise SpreadsheetLayoutError(
                f"{len(extra)} sheet(s) besides the first hold data; only one is expected."
            )

        sheet = workbook.worksheets[0]
        columns = find_columns(
            sheet.iter_rows(max_row=HEADER_SCAN_ROWS, values_only=True)
        )

        people: list[Person] = []
        rows = sheet.iter_rows(min_row=columns.header_row + 1, values_only=True)
        for row_number, values in enumerate(rows, start=columns.header_row + 1):
            dob = extract_dob(_cell(values, columns.dob))
            if dob is None:
                continue

            raw_name = _clean_text(_cell(values, columns.name))
            last, first, name_problem = parse_name(raw_name)
            problems = [p for p in (name_problem, check_dob(dob)) if p]
            people.append(
                Person(row_number, raw_name, last, first, dob, "; ".join(problems) or None)
            )
        return people
    finally:
        workbook.close()
