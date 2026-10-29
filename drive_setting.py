"""Show, confirm and change where HRS saves the day folders (paths.hearings_root).

The drive can move, so the person at the keyboard can change it without
opening config.yaml: from --setup, from --drive, or by answering C when a
double-clicked run shows where PDFs will go. Scheduled runs never ask.

The folder is printed to the console for that person only. It is never
logged, because logs get pasted.
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

import yaml

from exceptions import ConfigProblem

# The hearings_root line in config.yaml, indented under paths:. Only this one
# line is rewritten, so every comment and setting around it stays as written.
ROOT_LINE = re.compile(r"^(?P<indent>[ \t]+)hearings_root:[^\r\n]*", re.MULTILINE)


def read_root_setting(config_path: Path) -> str:
    """Return paths.hearings_root exactly as written in config.yaml.

    Used where the full config may not load yet, such as during --setup on
    a new computer, so nothing else in the file is checked.

    Raises:
        ConfigProblem: If the file is missing, does not parse, or has no
            paths.hearings_root.
    """
    if not config_path.is_file():
        raise ConfigProblem(
            "No config.yaml yet. Copy config.example.yaml to config.yaml beside "
            "HRS and fill it in."
        )
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigProblem(f"Could not parse config.yaml: {error}") from error
    paths = config.get("paths") if isinstance(config, dict) else None
    if not isinstance(paths, dict) or "hearings_root" not in paths:
        raise ConfigProblem("paths.hearings_root is missing from config.yaml.")
    return str(paths["hearings_root"] or "")


def resolve(setting: str, app_root: Path) -> Path:
    """Turn a hearings_root setting into the folder it means, as config_loader does."""
    path = Path(setting)
    return path if path.is_absolute() else app_root / path


def ask_for_folder(
    current: Path,
    app_root: Path,
    ask: Callable[[str], str] = input,
) -> str | None:
    """Ask for a new hearings folder. Returns it as typed, or None to keep current.

    A new folder must already exist, for the same reason HRS never creates
    the root: a typo would otherwise send PDFs somewhere nobody looks. The
    answer is kept as typed, so a relative folder stays relative in
    config.yaml.
    """
    print(f"Hearings folder now: {current}")
    while True:
        # Explorer's "Copy as path" wraps the path in double quotes.
        answer = ask("Press Enter to keep it, or type a new folder: ").strip().strip('"').strip()
        if not answer:
            return None
        if resolve(answer, app_root).is_dir():
            return answer
        print("That folder does not exist or cannot be reached. Try again, "
              "or press Enter to keep the current one.")


def save_root_setting(config_path: Path, setting: str) -> None:
    """Rewrite only the hearings_root line of config.yaml to hold setting.

    The value is written in single quotes, where a backslash is just a
    backslash, so a UNC path needs no escaping; a quote inside it is
    doubled, as YAML requires. The new text is parsed before it is saved,
    and saved by replacing the file whole, so a failure leaves config.yaml
    as it was.

    Raises:
        ConfigProblem: If the line is not found exactly once, the new file
            would not read back as setting, or the write fails.
    """
    # newline="" here and on the write keeps the file's own line endings.
    with config_path.open(encoding="utf-8", newline="") as file:
        text = file.read()
    matches = list(ROOT_LINE.finditer(text))
    if len(matches) != 1:
        raise ConfigProblem(
            "Could not find a single indented hearings_root line in config.yaml. "
            "Change it by hand."
        )
    quoted = "'" + setting.replace("'", "''") + "'"
    new_line = f"{matches[0]['indent']}hearings_root: {quoted}"
    new_text = text[: matches[0].start()] + new_line + text[matches[0].end():]

    try:
        parsed = yaml.safe_load(new_text)
        read_back = parsed["paths"]["hearings_root"]
    except (yaml.YAMLError, TypeError, KeyError) as error:
        raise ConfigProblem("The new folder would break config.yaml; it was not saved.") from error
    # Catches a hearings_root line that sits under some other section.
    if read_back != setting:
        raise ConfigProblem("The new folder would not read back from config.yaml; it was not saved.")

    temporary = config_path.with_name(config_path.name + ".new")
    try:
        temporary.write_text(new_text, encoding="utf-8", newline="")
        os.replace(temporary, config_path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigProblem(f"Could not save config.yaml: {type(error).__name__}") from error


def change_drive(config_path: Path, app_root: Path, ask: Callable[[str], str] = input) -> bool:
    """Show the hearings folder, offer to change it, and save a change.

    Returns True if config.yaml was changed.
    """
    current = resolve(read_root_setting(config_path), app_root)
    new = ask_for_folder(current, app_root, ask)
    if new is None:
        print("Kept.")
        return False
    save_root_setting(config_path, new)
    print(f"Saved. PDFs will go under: {resolve(new, app_root)}")
    return True


def confirm_drive(
    root: Path, config_path: Path, app_root: Path, ask: Callable[[str], str] = input
) -> bool:
    """Show where PDFs will go and let the person continue or change it.

    Used on a double-clicked run. Returns True if config.yaml was changed,
    so the caller loads it again.
    """
    print(f"PDFs will be saved under: {root}")
    answer = ask("Press Enter to continue, or C to change: ").strip().lower()
    if answer != "c":
        return False
    return change_drive(config_path, app_root, ask)
