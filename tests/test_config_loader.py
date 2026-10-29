"""Tests for config_loader, built from config.example.yaml with fake values.

Credential Manager is never touched: load_credentials is replaced per test.
"""

import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_loader  # noqa: E402
from config_loader import APP_ROOT, load_config  # noqa: E402
from credentials import MissingCredentials  # noqa: E402
from exceptions import ConfigProblem  # noqa: E402

EXAMPLE = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text(encoding="utf-8")

# The example's placeholders, swapped for fake values that pass validation.
FILLED = (
    EXAMPLE.replace("https://ccis.example.com", "https://ccis.test.invalid")
    .replace("PASTE-TENANT-ID", "fake-tenant")
    .replace("PASTE-CLIENT-ID", "fake-client")
    .replace("mailbox@example.com", "mailbox@test.invalid")
    .replace("sender@example.com", "sender@test.invalid")
    .replace("PASTE-SUBJECT-PHRASE", "Daily Bookings")
)

SECRETS = {
    "ccis_username": "fake-user",
    "ccis_password": "fake-pass",
    "graph_client_secret": "fake-secret",
}


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets the fake secrets unless it replaces them."""
    monkeypatch.setattr(config_loader, "load_credentials", lambda: dict(SECRETS))


def _write(tmp_path: Path, text: str) -> Path:
    """Save text as a config.yaml and return its path."""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_filled_example_loads(tmp_path: Path) -> None:
    """The example, filled in, passes, with secrets and parsed values added."""
    config = load_config(_write(tmp_path, FILLED))

    assert config["ccis"]["username"] == "fake-user"
    assert config["ccis"]["password"] == "fake-pass"
    assert config["graph"]["client_secret"] == "fake-secret"
    assert config["graph"]["check_until"] == time(8, 0)
    assert config["graph"]["check_every_minutes"] == 10
    assert config["paths"]["hearings_root"] == APP_ROOT / "test"
    assert config["paths"]["chromedriver"] is None


def test_unfilled_example_is_refused(tmp_path: Path) -> None:
    """A config.yaml copied but not filled in stops at the first placeholder."""
    with pytest.raises(ConfigProblem, match="not filled in"):
        load_config(_write(tmp_path, EXAMPLE))


def test_missing_file_names_the_fix(tmp_path: Path) -> None:
    """No config.yaml says to copy the example."""
    with pytest.raises(ConfigProblem, match="config.example.yaml"):
        load_config(tmp_path / "config.yaml")


@pytest.mark.parametrize("value", ["10:30", '"9am"', '"25:00"', '""'])
def test_check_until_must_be_a_quoted_time(tmp_path: Path, value: str) -> None:
    """Unquoted 10:30 is the number 630 to YAML, and nonsense times are refused."""
    text = FILLED.replace('check_until: "08:00"', f"check_until: {value}")

    with pytest.raises(ConfigProblem, match="check_until"):
        load_config(_write(tmp_path, text))


def test_check_until_accepts_other_times(tmp_path: Path) -> None:
    """Any 24-hour time in quotes works."""
    text = FILLED.replace('check_until: "08:00"', 'check_until: "10:30"')

    assert load_config(_write(tmp_path, text))["graph"]["check_until"] == time(10, 30)


@pytest.mark.parametrize("value", ["0", "-5", '"5"'])
def test_check_every_minutes_must_be_one_or_more(tmp_path: Path, value: str) -> None:
    """Zero would check Graph in a tight loop."""
    text = FILLED.replace("check_every_minutes: 10", f"check_every_minutes: {value}")

    with pytest.raises(ConfigProblem, match="check_every_minutes"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("line, name", [
    ('  credential_entry: "rap-sheet-ccis"\n', "ccis.credential_entry"),
    ('  password: "x"\n', "ccis.password"),
])
def test_retired_ccis_settings_are_refused(tmp_path: Path, line: str, name: str) -> None:
    """An old config.yaml line is named so it can be deleted."""
    text = FILLED.replace("ccis:\n", "ccis:\n" + line, 1)

    with pytest.raises(ConfigProblem, match=name):
        load_config(_write(tmp_path, text))


def test_client_secret_in_file_is_refused(tmp_path: Path) -> None:
    """A secret pasted into config.yaml is refused."""
    text = FILLED.replace("graph:\n", 'graph:\n  client_secret: "x"\n', 1)

    with pytest.raises(ConfigProblem, match="graph.client_secret"):
        load_config(_write(tmp_path, text))


def test_missing_secrets_reach_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MissingCredentials comes through as itself, so main can offer setup."""
    def missing() -> dict:
        raise MissingCredentials("none stored")

    monkeypatch.setattr(config_loader, "load_credentials", missing)

    with pytest.raises(MissingCredentials):
        load_config(_write(tmp_path, FILLED))


def test_file_is_checked_before_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken config.yaml is reported even with no secrets stored."""
    def missing() -> dict:
        raise MissingCredentials("none stored")

    monkeypatch.setattr(config_loader, "load_credentials", missing)

    with pytest.raises(ConfigProblem, match="not filled in"):
        load_config(_write(tmp_path, EXAMPLE))
