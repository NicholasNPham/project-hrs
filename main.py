"""Run HRS: read the booking spreadsheet, search CCIS for each person, save PDFs.

    python main.py <spreadsheet.xlsx>             search everyone
    python main.py <spreadsheet.xlsx> --limit 3   search the first 3 only
    python main.py <spreadsheet.xlsx> --check     read the sheet only, no CCIS
    python main.py --email                        use today's booking email
    python main.py --email --check                fetch it and read it, no CCIS
    python main.py --email --force                run again on a day already done
    python main.py                                like --email, but asks first (double-click)
    python main.py --setup                        store secrets, confirm the drive folder
    python main.py --drive                        confirm or change the drive folder only

Add --headless or --no-headless to any run to override ccis.headless for
that run only; config.yaml is not changed.

With no arguments, as when HRS.exe is double-clicked, HRS is run by a person
at the screen: it offers setup if this Windows user has no secrets stored,
shows where PDFs will go with a chance to change it, and waits for Enter
before closing so the summary can be read. Every other run
never asks anything, so a scheduled task given --email cannot hang on a
prompt. Task Scheduler can show a console window, so whether someone is
there is decided by the arguments, not by whether a console exists.

A full --email run writes "HRS complete.txt" into the day folder, and later
--email runs that day stop at once. Only one HRS runs at a time per computer.

--email saves the spreadsheet as logs/YYYY-MM-DD.xlsx, beside that day's
log, and works from that copy. If today's email has not arrived, it checks
again every graph.check_every_minutes until graph.check_until, then stops.
--limit works with either source.

Console and log lines name spreadsheet rows, never people, so either is safe
to paste. The PDFs themselves are named LAST_FIRST.pdf and go into
<hearings_root>/<mm.dd.yyyy>/CCIS, so a trial root inside the project must
be gitignored, as test/ is.
"""

import argparse
import re
import shutil
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

from openpyxl import load_workbook

from ccis import CcisSession
from config_loader import APP_ROOT, CONFIG_PATH, load_config
from credentials import MissingCredentials, run_setup
from drive_setting import change_drive, confirm_drive
from excel_reader import HEADER_SCAN_ROWS, SUFFIXES, Person, find_columns, read_people
from exceptions import ConfigProblem, RowProblem, SystemProblem
from folders import check_root, day_folder, prepare_day_folder
from graph_client import BookingSheet, EmailNotArrived, GraphClient
from logger import get_logger, setup_logging
from run_guard import finished_today, mark_finished, single_run

# Anything outside these is replaced in file names. Windows refuses some
# characters outright, and a stray one would fail the write for that person.
UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9'-]+")

HISTORY = "criminal history"
NO_RESULTS = "no results found"
NEEDS_A_LOOK = "needs a look by hand"


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


def pdf_name(person: Person, used: set[str]) -> str:
    """Return LAST_FIRST.pdf, adding _2, _3 when two people in a run share it.

    A file left from an earlier run with the same name is overwritten, so a
    rerun replaces its own PDFs instead of piling up copies.
    """
    stem = "_".join(
        UNSAFE_FILENAME_CHARS.sub("-", part).strip("-").upper()
        for part in (person.last, person.first)
    )
    name = f"{stem}.pdf"
    copy = 2
    while name in used:
        name = f"{stem}_{copy}.pdf"
        copy += 1
    used.add(name)
    return name


def search_people(
    config: dict, people: list[Person], pdf_dir: Path
) -> dict[str, list[int]]:
    """Search CCIS for each person and save their PDF into pdf_dir.

    Returns the spreadsheet rows under each outcome. A RowProblem sends that
    person to "needs a look by hand" and the run carries on; a SystemProblem
    stops it.
    """
    log = get_logger()
    outcomes: dict[str, list[int]] = {HISTORY: [], NO_RESULTS: [], NEEDS_A_LOOK: []}
    used: set[str] = set()
    with CcisSession(config) as ccis:
        for number, person in enumerate(people, start=1):
            try:
                result = ccis.fetch_pdf(person.first, person.last, person.dob)
            except RowProblem as error:
                log.warning("Row %d (%d of %d): %s: %s",
                            person.row, number, len(people), NEEDS_A_LOOK, error)
                outcomes[NEEDS_A_LOOK].append(person.row)
                continue

            try:
                (pdf_dir / pdf_name(person, used)).write_bytes(result.pdf_bytes)
            except OSError as error:
                # A folder that cannot be written to fails every person after
                # this one too, so it stops the run.
                raise SystemProblem(
                    f"Could not save the PDF for row {person.row}: {type(error).__name__}"
                ) from error

            outcome = HISTORY if result.has_record else NO_RESULTS
            log.info("Row %d (%d of %d): %s", person.row, number, len(people), outcome)
            outcomes[outcome].append(person.row)
    return outcomes


def start(interactive: bool = False, headless: bool | None = None) -> tuple[dict, date]:
    """Load config and start the day's log. Returns the config and the run date.

    One date serves the log, the email search and the day folder, so a run
    that crosses midnight does not split across two days.

    When interactive, missing secrets start setup instead of stopping, and
    the drive folder is shown for the person to confirm or change. Both
    happen before logging starts, so the folder never reaches the log.
    headless, when given, replaces ccis.headless for this run only.
    """
    try:
        config = load_config()
    except MissingCredentials as error:
        if not interactive:
            raise
        print(error)
        print("Starting setup.")
        run_setup()
        config = load_config()
    if interactive and confirm_drive(config["paths"]["hearings_root"], CONFIG_PATH, APP_ROOT):
        config = load_config()
    # After every load above, so a reload cannot undo it.
    if headless is not None:
        config["ccis"]["headless"] = headless
    today = date.today()
    setup_logging(config["paths"]["logs"] / f"{today:%Y-%m-%d}.log")
    return config, today


def wait_for_email(
    fetch: Callable[[], BookingSheet],
    today: date,
    until: clock,
    every: timedelta,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
) -> BookingSheet:
    """Call fetch until the email has arrived or until today at `until`.

    Only EmailNotArrived is waited out; every other problem stops at once.
    The last wait is cut short so one final check lands on the deadline
    itself, rather than giving up early because a full wait would overrun
    it. Started after the deadline, it checks once.

    Raises:
        SystemProblem: If no email has arrived by the deadline, or fetch
            fails any other way.
    """
    log = get_logger()
    deadline = datetime.combine(today, until)
    while True:
        try:
            return fetch()
        except EmailNotArrived as error:
            remaining = deadline - now()
            if remaining <= timedelta(0):
                # The last check's reason says which setting to look at.
                raise SystemProblem(
                    f"No booking email arrived by {until:%H:%M}. Nothing was searched. "
                    f"{error}"
                ) from error
            wait = min(every, remaining)
            log.info("No booking email yet, checking again at %s",
                     f"{now() + wait:%H:%M}")
            sleep(wait.total_seconds())


def fetch_from_email(config: dict, today: date) -> tuple[Path, str]:
    """Download today's booking spreadsheet and save it beside today's log.

    Waits for the email as set in config. A rerun the same day overwrites
    the copy with the same email's sheet. Returns the saved copy and the
    file name the sheet had on the email.
    """
    graph = config["graph"]
    client = GraphClient(
        graph["tenant_id"], graph["client_id"], graph["client_secret"], graph["mailbox"]
    )
    sheet = wait_for_email(
        lambda: client.fetch_booking_sheet(today, graph["sender"], graph["subject_contains"]),
        today,
        graph["check_until"],
        timedelta(minutes=graph["check_every_minutes"]),
    )
    path = config["paths"]["logs"] / f"{today:%Y-%m-%d}.xlsx"
    try:
        path.write_bytes(sheet.content)
    except OSError as error:
        raise SystemProblem(
            f"Could not save the emailed spreadsheet: {type(error).__name__}"
        ) from error
    get_logger().info("Spreadsheet saved beside today's log")
    return path, sheet.name


def copy_sheet_to_drive(source: Path, target: Path) -> None:
    """Copy the emailed sheet into the day folder beside the four folders.

    A failed copy is logged and the run goes on: the PDFs are the work, and
    the sheet is still in logs. A rerun the same day overwrites the copy.
    """
    try:
        shutil.copyfile(source, target)
    except OSError as error:
        get_logger().warning(
            "Could not copy the spreadsheet to the day folder: %s", type(error).__name__
        )
        return
    get_logger().info("Spreadsheet copied to the day folder")


def run(
    path: Path | None,
    limit: int | None,
    force: bool = False,
    interactive: bool = False,
    headless: bool | None = None,
) -> int:
    """Load config, read the sheet, search everyone ready, and print a summary.

    With no path, the sheet comes from today's booking email, and the run
    is guarded so each day is searched once: a day already marked done is
    skipped unless force is set, and only a full email run marks it done.
    A run from a local file is never skipped and never marks the day,
    since that file may not be today's sheet.
    """
    config, today = start(interactive, headless)
    with single_run(config["paths"]["logs"]):
        return _guarded_run(config, today, path, limit, force)


def _guarded_run(
    config: dict, today: date, path: Path | None, limit: int | None, force: bool
) -> int:
    """The body of run, inside the lock."""
    log = get_logger()
    root = config["paths"]["hearings_root"]
    # Before the email wait, so a dropped drive is reported at 5:30, not 9:00.
    check_root(root)
    folder = day_folder(root, today)
    from_email = path is None
    # Only the emailed sheet is copied to the drive. A local file may not be
    # today's sheet, the same reason it never marks the day done.
    sheet_name = None

    if from_email and finished_today(folder):
        if not force:
            log.info("Already ran today. Next run is tomorrow. "
                     "Use --force to run today again.")
            return 0
        log.warning("Today is already marked done; running again because of --force")

    if from_email:
        path, sheet_name = fetch_from_email(config, today)

    people = read_people(path)
    ready = [p for p in people if not p.problem]
    flagged = [p for p in people if p.problem]
    log.info("People found: %d, ready: %d, flagged: %d",
             len(people), len(ready), len(flagged))
    for person in flagged:
        log.warning("Row %d: not searched: %s", person.row, person.problem)
    if limit is not None:
        ready = ready[:limit]
        log.info("Limited to the first %d ready", len(ready))

    # Before Chrome opens, so an unreachable drive stops the run without a
    # wasted sign-in.
    pdf_dir = prepare_day_folder(config["paths"]["hearings_root"], today)
    log.info("Day folder ready")
    if sheet_name:
        copy_sheet_to_drive(path, folder / sheet_name)

    outcomes = search_people(config, ready, pdf_dir)

    print()
    print(f"Searched:             {len(ready)}")
    for outcome, rows in outcomes.items():
        print(f"{outcome + ':':<22}{len(rows)}")
    for row in outcomes[NEEDS_A_LOOK]:
        print(f"  row {row}")
    print(f"Flagged, not searched: {len(flagged)}")
    for person in flagged:
        print(f"  row {person.row}: {person.problem}")

    # A --limit run searched only some people, so the day is not done.
    if from_email and limit is None:
        counts = ", ".join(f"{outcome} {len(rows)}" for outcome, rows in outcomes.items())
        mark_finished(
            folder,
            f"Searched {len(ready)}: {counts}. Flagged, not searched: {len(flagged)}.",
            datetime.now(),
        )
        log.info("Today marked done")
    return 0


def setup() -> int:
    """Store secrets, confirm the drive folder, then check that everything loads.

    Returns 0 when config.yaml and the secrets are complete, 1 when
    something is still missing, so a person setting up a new computer is
    told what is left rather than finding out at 5:30 the next morning.
    """
    run_setup()
    print()
    try:
        change_drive(CONFIG_PATH, APP_ROOT)
    except ConfigProblem as error:
        print(f"Drive folder not set: {error}")
    print()
    try:
        load_config()
    except ConfigProblem as error:
        print(f"Still to fix: {error}")
        return 1
    print("config.yaml and the secrets are complete.")
    return 0


def pause_before_closing() -> None:
    """Wait for Enter, so a double-clicked window does not vanish with the summary."""
    try:
        input("\nPress Enter to close.")
    except (KeyboardInterrupt, EOFError):
        pass


def report_stop(message: str) -> None:
    """Show why the run stopped, and put it in today's log once there is one.

    A scheduled run has nobody watching its window, so the log is the only
    place the reason survives. Stops before logging starts, such as a bad
    config.yaml, can only be printed; --setup reports those.
    """
    log = get_logger()
    if log.handlers:
        # The log's console handler prints it too.
        log.error(message)
    else:
        print(message)


def main(argv: list[str] | None = None) -> int:
    """Run the command line, pausing at the end when someone double-clicked."""
    argv = sys.argv[1:] if argv is None else argv
    # Double-clicked: a person is at the screen to answer and to read the end.
    interactive = not argv and sys.stdin.isatty()
    try:
        code = _command(argv, interactive)
    except Exception as error:
        # Only the type and the place go in the log. The message of an
        # unexpected error was never checked for what it might quote.
        where = traceback.extract_tb(error.__traceback__)[-1]
        report_stop(
            f"Stopped on an unexpected {type(error).__name__} at "
            f"{Path(where.filename).name}:{where.lineno}"
        )
        # A scheduled run lets the traceback through, so the task shows a
        # failure. A double-clicked one shows it and waits, since the window
        # would otherwise close before anyone could read it.
        if not interactive:
            raise
        traceback.print_exc()
        code = 1
    if interactive:
        pause_before_closing()
    return code


def _command(argv: list[str], interactive: bool) -> int:
    """Parse the command line and run, turning a SystemProblem into exit code 1."""
    parser = argparse.ArgumentParser(description="Search CCIS for everyone on a booking sheet.")
    parser.add_argument("spreadsheet", type=Path, nargs="?")
    parser.add_argument("--email", action="store_true", help="use today's booking email")
    parser.add_argument("--limit", type=int, help="search only the first N ready people")
    parser.add_argument("--check", action="store_true", help="read the sheet only, no CCIS")
    parser.add_argument("--force", action="store_true", help="run again on a day already done")
    parser.add_argument("--setup", action="store_true",
                        help="store secrets and confirm the drive folder")
    parser.add_argument("--drive", action="store_true",
                        help="confirm or change the drive folder")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction,
                        help="override ccis.headless for this run only")
    args = parser.parse_args(argv)

    if args.setup or args.drive:
        if len(argv) != 1:
            print("--setup and --drive are used on their own.")
            return 2
        try:
            if args.setup:
                return setup()
            change_drive(CONFIG_PATH, APP_ROOT)
            return 0
        except SystemProblem as error:
            print(f"Stopped: {error}")
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1

    # Double-clicked: today's email is the source.
    if not argv:
        args.email = True

    if args.email == (args.spreadsheet is not None):
        print("Give a spreadsheet or --email, one or the other.")
        return 2
    if args.spreadsheet is not None and not args.spreadsheet.is_file():
        print("File not found.")
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit must be 1 or more.")
        return 2
    if args.force and not args.email:
        print("--force only applies to --email runs.")
        return 2

    try:
        if args.check:
            path = args.spreadsheet
            if args.email:
                path = fetch_from_email(*start())
            describe_layout(path)
            describe_people(read_people(path))
            return 0
        return run(args.spreadsheet, args.limit, args.force, interactive, args.headless)
    except SystemProblem as error:
        report_stop(f"Stopped: {error}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        report_stop("Cancelled.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
