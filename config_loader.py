"""Read config.yaml and HRS's secrets, and refuse anything missing.

There are no defaults on purpose: a missing or placeholder setting stops the
run at startup, not halfway through a list of people. Secrets come from
Windows Credential Manager through credentials.py, never from the file.
"""

import sys
from datetime import datetime
from datetime import time as clock
from pathlib import Path

import yaml

from credentials import load_credentials
from exceptions import ConfigProblem

# Built as HRS.exe, the code runs from PyInstaller's bundle, not from a folder
# anyone edits. config.yaml, logs and a relative hearings_root belong beside
# the exe instead, where the person who installed it can find them.
if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = APP_ROOT / "config.yaml"

# Values config.example.yaml ships with. Seeing one means config.yaml was
# copied but not filled in.
PLACEHOLDERS = frozenset({
    "",
    "https://ccis.example.com",
    "mailbox@example.com",
    "sender@example.com",
    "PASTE-TENANT-ID",
    "PASTE-CLIENT-ID",
    "PASTE-SUBJECT-PHRASE",
})

# Settings that moved to Credential Manager or were dropped. Seeing one means
# an old config.yaml, which would otherwise suggest it still does something.
RETIRED = {
    "ccis": ("username", "password", "credential_entry"),
    "graph": ("client_secret",),
}


def _section(config: dict, name: str) -> dict:
    """Return a top-level section, or raise if it is missing."""
    section = config.get(name)
    if not isinstance(section, dict):
        raise ConfigProblem(f"config.yaml has no '{name}' section.")
    return section


def _text(section: dict, key: str, where: str, allow_blank: bool = False) -> str:
    """Return a string setting, refusing a missing key or a placeholder."""
    if key not in section:
        raise ConfigProblem(f"{where}.{key} is missing from config.yaml.")
    value = str(section[key] or "").strip()
    if value in PLACEHOLDERS and not (allow_blank and value == ""):
        raise ConfigProblem(f"{where}.{key} is not filled in in config.yaml.")
    return value


def _number(section: dict, key: str, where: str) -> float:
    """Return a setting that must be a number zero or above."""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigProblem(f"{where}.{key} must be a number, 0 or more.")
    return float(value)


def _flag(section: dict, key: str, where: str) -> bool:
    """Return a setting that must be true or false."""
    value = section.get(key)
    if not isinstance(value, bool):
        raise ConfigProblem(f"{where}.{key} must be true or false.")
    return value


def _time_of_day(section: dict, key: str, where: str) -> clock:
    """Return a setting that must be a quoted 24-hour time such as "09:00"."""
    value = section.get(key)
    # Unquoted, YAML reads 10:30 as the number 630, so only a string is taken.
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%H:%M").time()
        except ValueError:
            pass
    raise ConfigProblem(f'{where}.{key} must be a 24-hour time in quotes, like "09:00".')


def _path(value: str) -> Path:
    """Resolve a path from config against the folder HRS runs from."""
    path = Path(value)
    return path if path.is_absolute() else APP_ROOT / path


def _address(section: dict, key: str, where: str) -> str:
    """Return an email address setting, refusing one without a single @."""
    value = _text(section, key, where)
    # The address is left out of the message because it reaches the console.
    if value.count("@") != 1:
        raise ConfigProblem(f"{where}.{key} in config.yaml is not an email address.")
    return value


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read and validate config.yaml, and add the secrets from Credential Manager.

    Returns the config with ccis.username, ccis.password and
    graph.client_secret filled in, every setting checked, and paths resolved
    to absolute Path objects. The file is checked before Credential Manager,
    so a broken config.yaml is reported even on a computer with no secrets.

    Raises:
        credentials.MissingCredentials: If secrets are not stored for this
            Windows user, so the caller can offer setup.
        ConfigProblem: For anything else wrong.
    """
    if not path.is_file():
        raise ConfigProblem(
            f"No config file at {path}. Copy config.example.yaml to config.yaml "
            "in that folder and fill it in."
        )
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigProblem(f"Could not parse config.yaml: {error}") from error
    if not isinstance(config, dict):
        raise ConfigProblem("config.yaml does not hold a YAML mapping.")

    for name, keys in RETIRED.items():
        section = config.get(name)
        leftovers = [
            f"{name}.{key}" for key in keys if isinstance(section, dict) and key in section
        ]
        if leftovers:
            raise ConfigProblem(
                f"{' and '.join(leftovers)} is in config.yaml but no longer used. "
                "Secrets are stored with HRS --setup; delete those lines."
            )

    ccis = _section(config, "ccis")
    ccis["url"] = _text(ccis, "url", "ccis").rstrip("/")
    ccis["headless"] = _flag(ccis, "headless", "ccis")
    for key in ("wait_timeout", "no_results_wait_timeout", "pause_between_actions"):
        ccis[key] = _number(ccis, key, "ccis")
    locators = ccis.get("locators") or {}
    if not isinstance(locators, dict):
        raise ConfigProblem("ccis.locators must be a mapping of name to locator.")
    ccis["locators"] = locators

    graph = _section(config, "graph")
    for key in ("tenant_id", "client_id", "subject_contains"):
        graph[key] = _text(graph, key, "graph")
    graph["mailbox"] = _address(graph, "mailbox", "graph")
    graph["sender"] = _address(graph, "sender", "graph")
    graph["check_until"] = _time_of_day(graph, "check_until", "graph")
    graph["check_every_minutes"] = _number(graph, "check_every_minutes", "graph")
    if graph["check_every_minutes"] < 1:
        raise ConfigProblem("graph.check_every_minutes must be 1 or more.")

    paths = _section(config, "paths")
    paths["hearings_root"] = _path(_text(paths, "hearings_root", "paths"))
    paths["logs"] = _path(_text(paths, "logs", "paths"))
    chromedriver = _text(paths, "chromedriver", "paths", allow_blank=True)
    paths["chromedriver"] = _path(chromedriver) if chromedriver else None

    secrets = load_credentials()
    ccis["username"] = secrets["ccis_username"]
    ccis["password"] = secrets["ccis_password"]
    graph["client_secret"] = secrets["graph_client_secret"]
    return config
