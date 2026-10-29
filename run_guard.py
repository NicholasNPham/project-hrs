"""Keep HRS to one run at a time and one finished run per day.

Two guards, for two different mistakes:

- A lock file held for the whole run stops a second copy starting while one
  is still going, for example a double-click during the scheduled run.
- A done marker in the day's folder on the drive stops a second full run
  once one has finished. It is on the drive, not on one computer, so a run
  from a different PC sees it too. Deleting it allows a rerun, and so does
  --force.
"""

import msvcrt
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from exceptions import SystemProblem

DONE_MARKER = "HRS complete.txt"
LOCK_FILE = "hrs.lock"


class AlreadyRunning(SystemProblem):
    """Another copy of HRS holds the lock on this computer."""


@contextmanager
def single_run(folder: Path) -> Iterator[None]:
    """Hold folder/hrs.lock for the duration of the with block.

    Windows releases the lock when the process exits, even after a crash or
    a closed window, so there is never a stale lock to clean up. The file
    itself is left behind on purpose; only the lock on it matters.

    Raises:
        AlreadyRunning: If another HRS process holds the lock.
        SystemProblem: If the lock file cannot be opened.
    """
    try:
        handle = open(folder / LOCK_FILE, "a+")
    except OSError as error:
        raise SystemProblem(f"Could not open the lock file: {type(error).__name__}") from error
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise AlreadyRunning(
                "HRS is already running on this computer. Let that run finish."
            ) from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def finished_today(day_folder: Path) -> bool:
    """Check whether a full run already finished for this day folder."""
    return (day_folder / DONE_MARKER).is_file()


def mark_finished(day_folder: Path, summary: str, finished: datetime) -> None:
    """Write the done marker. Only counts and times go in it, never names.

    Raises:
        SystemProblem: If the marker cannot be written. The PDFs are already
            saved by then, but without the marker the next run would search
            everyone again, so it is reported as a failure.
    """
    text = (
        f"HRS finished a full run at {finished:%m/%d/%Y %H:%M}.\n"
        f"{summary}\n"
        "Delete this file, or run HRS with --force, to run today again.\n"
    )
    try:
        (day_folder / DONE_MARKER).write_text(text, encoding="utf-8")
    except OSError as error:
        raise SystemProblem(
            f"Searches finished, but the done marker could not be written: "
            f"{type(error).__name__}. The next run today will search everyone again."
        ) from error
