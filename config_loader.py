"""Read config.yaml and the CCIS login, and refuse anything missing.

There are no defaults on purpose: a missing or placeholder setting stops the
run at startup, not halfway through a list of people. The login comes from
Windows Credential Manager, never from the file.
"""

from pathlib import Path

import keyring
import yaml
from keyring.errors import KeyringError

from exceptions import ConfigProblem

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Values config.example.yaml ships with. Seeing one means config.yaml was
# copied but not filled in.
PLACEHOLDERS = frozenset({"", "https://ccis.example.com"})


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


def _path(value: str) -> Path:
    """Resolve a path from config against the project root."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_login(entry: str) -> tuple[str, str]:
    """Read the username and password stored under one Credential Manager entry."""
    try:
        credential = keyring.get_credential(entry, None)
    except KeyringError as error:
        raise ConfigProblem(
            f"Could not read '{entry}' from Windows Credential Manager: {error}"
        ) from error
    if credential is None or not credential.username or not credential.password:
        raise ConfigProblem(
            f"No login stored under '{entry}' in Windows Credential Manager. "
            "Run Project-Rap-Sheet's store_secret.py as this Windows account."
        )
    return credential.username, credential.password


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read and validate config.yaml, and add the CCIS login from Credential Manager.

    Returns the config with ccis.username and ccis.password filled in, every
    setting checked, and paths resolved to absolute Path objects.
    """
    if not path.is_file():
        raise ConfigProblem(
            f"No config file at {path}. Copy config.example.yaml to config.yaml "
            "and fill it in."
        )
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigProblem(f"Could not parse config.yaml: {error}") from error
    if not isinstance(config, dict):
        raise ConfigProblem("config.yaml does not hold a YAML mapping.")

    ccis = _section(config, "ccis")
    leftovers = [key for key in ("username", "password") if key in ccis]
    if leftovers:
        raise ConfigProblem(
            f"ccis.{' and '.join(leftovers)} is in config.yaml. Logins live in "
            "Windows Credential Manager; delete those lines."
        )
    ccis["url"] = _text(ccis, "url", "ccis").rstrip("/")
    ccis["credential_entry"] = _text(ccis, "credential_entry", "ccis")
    ccis["headless"] = _flag(ccis, "headless", "ccis")
    for key in ("wait_timeout", "no_results_wait_timeout", "pause_between_actions"):
        ccis[key] = _number(ccis, key, "ccis")
    locators = ccis.get("locators") or {}
    if not isinstance(locators, dict):
        raise ConfigProblem("ccis.locators must be a mapping of name to locator.")
    ccis["locators"] = locators
    ccis["username"], ccis["password"] = _read_login(ccis["credential_entry"])

    paths = _section(config, "paths")
    paths["hearings_root"] = _path(_text(paths, "hearings_root", "paths"))
    paths["logs"] = _path(_text(paths, "logs", "paths"))
    chromedriver = _text(paths, "chromedriver", "paths", allow_blank=True)
    paths["chromedriver"] = _path(chromedriver) if chromedriver else None

    return config
