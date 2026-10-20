"""Prepare the day's folder on the network drive.

Each day gets one folder under the hearings root, named for the date, with
four subfolders. HRS fills only CCIS; the other three are made empty for
staff to fill by hand.
"""

from datetime import date
from pathlib import Path

from exceptions import SystemProblem

CCIS_FOLDER = "CCIS"
SUBFOLDERS = ("Affidavit", CCIS_FOLDER, "PTR", "VIC")

# Windows does not allow slashes in a folder name.
DATE_FOLDER_FORMAT = "%m.%d.%Y"


def prepare_day_folder(root: Path, day: date) -> Path:
    """Create root/<date>/ with its four subfolders and return the CCIS folder.

    An existing day folder is reused, so a second run on the same day adds to
    it instead of stopping. The root itself must already exist: creating it
    would hide a disconnected or mistyped drive by building a new tree in the
    wrong place.
    """
    # The path is left out of messages because they reach the console and logs.
    if not root.is_dir():
        raise SystemProblem(
            "The hearings folder in config.yaml cannot be reached. "
            "Check that the drive is connected."
        )
    day_folder = root / day.strftime(DATE_FOLDER_FORMAT)
    try:
        for name in SUBFOLDERS:
            (day_folder / name).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SystemProblem(
            f"Could not create today's folders: {type(error).__name__}"
        ) from error
    return day_folder / CCIS_FOLDER
