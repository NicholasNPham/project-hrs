"""Check that a real booking spreadsheet reads correctly.

Prints only counts, row numbers and reasons, never a name, DOB or any other
cell value, so the output is safe to paste into a chat or a ticket. This
becomes the full run once the CCIS and folder stages exist.
"""

import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from excel_reader import (
    HEADER_SCAN_ROWS,
    SUFFIXES,
    Person,
    SpreadsheetLayoutError,
    find_columns,
    read_people,
)


def describe_layout(path: Path) -> None:
    """Print the header position, chosen columns and data row count."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        columns = find_columns(
            sheet.iter_rows(max_row=HEADER_SCAN_ROWS, values_only=True)
        )
        data_rows = sum(1 for _ in sheet.iter_rows(min_row=columns.header_row + 1))
        print(f"Sheets in workbook:  {len(workbook.worksheets)} (reading the first)")
        print(f"Header row:          {columns.header_row}")
        print(f"Name column:         {columns.name + 1}")
        print(f"DOB column:          {columns.dob + 1}")
        print(f"Rows below header:   {data_rows}")
    finally:
        workbook.close()


def name_shape(person: Person) -> str:
    """Describe a name's form without revealing it."""
    # A suffix after a second comma counts as part of the first-name side.
    last_part, first_part = person.raw_name.split(",", 1)
    first_part = first_part.replace(",", " ")
    tokens = [t.rstrip(".").upper() for t in (last_part + " " + first_part).split()]
    parts = [f"{len(last_part.split())}-word last", f"{len(first_part.split())}-word first"]
    if any(t in SUFFIXES for t in tokens):
        parts.append("suffix")
    return ", ".join(parts)


def describe_people(people: list[Person]) -> None:
    """Print how many people were found, the problem rows and name shapes."""
    problems = [p for p in people if p.problem]
    good = [p for p in people if not p.problem]

    print(f"People found:        {len(people)}")
    print(f"Ready to search:     {len(good)}")
    print(f"Flagged:             {len(problems)}")
    for person in problems:
        print(f"  row {person.row}: {person.problem}")

    print("Name shapes (count):")
    for shape, count in sorted(Counter(name_shape(p) for p in good).items()):
        print(f"  {count:>4}  {shape}")


def main() -> int:
    """Read the spreadsheet named on the command line and print a summary."""
    if len(sys.argv) != 2:
        print("Usage: python main.py <booking spreadsheet .xlsx>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print("File not found.")
        return 2

    try:
        describe_layout(path)
        describe_people(read_people(path))
    except SpreadsheetLayoutError as error:
        print(f"Layout problem: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
