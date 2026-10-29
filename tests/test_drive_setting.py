"""Tests for drive_setting: confirming and changing paths.hearings_root.

Every folder is under pytest's tmp_path; no real drive is touched.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drive_setting import (  # noqa: E402
    ask_for_folder,
    change_drive,
    confirm_drive,
    read_root_setting,
    save_root_setting,
)
from exceptions import ConfigProblem  # noqa: E402

CONFIG = (
    "# top comment\n"
    "ccis:\n"
    "  headless: false\n"
    "paths:\n"
    "  # where the day folders go\n"
    '  hearings_root: "test"\n'
    '  logs: "logs"\n'
)


def _answers(*replies: str) -> Callable[[str], str]:
    """A fake input() that gives these replies in order and fails if asked more."""
    queue = list(replies)

    def ask(prompt: str) -> str:
        assert queue, f"asked more than expected: {prompt}"
        return queue.pop(0)

    return ask


def _write(path: Path, text: str, newline: str = "\n") -> Path:
    """Write config text with the given line ending."""
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """A small config.yaml in tmp_path."""
    return _write(tmp_path / "config.yaml", CONFIG)


# Saving


@pytest.mark.parametrize("setting", [
    r"\\server\share\First Appearance Hearings",
    r"G:\First Appearance Hearings",
    r"\\server\share\O'Brien's folder",
    "test",
])
def test_saved_value_reads_back_exactly(config_path: Path, setting: str) -> None:
    """Backslashes and quotes survive the round trip through YAML."""
    save_root_setting(config_path, setting)

    assert read_root_setting(config_path) == setting


def test_only_the_root_line_changes(config_path: Path) -> None:
    """Comments and other settings stay as written."""
    save_root_setting(config_path, r"\\server\share")

    assert config_path.read_text(encoding="utf-8") == CONFIG.replace(
        '"test"', r"'\\server\share'"
    )


def test_windows_line_endings_are_kept(tmp_path: Path) -> None:
    """A CRLF file stays CRLF."""
    path = _write(tmp_path / "config.yaml", CONFIG, newline="\r\n")

    save_root_setting(path, "elsewhere")

    raw = path.read_bytes()
    assert raw.count(b"\r\n") == CONFIG.count("\n")
    assert b"hearings_root: 'elsewhere'\r\n" in raw


def test_missing_root_line_is_refused(tmp_path: Path) -> None:
    """With no line to replace, nothing is written."""
    path = _write(tmp_path / "config.yaml", "paths:\n  logs: logs\n")

    with pytest.raises(ConfigProblem, match="by hand"):
        save_root_setting(path, "elsewhere")
    assert path.read_text(encoding="utf-8") == "paths:\n  logs: logs\n"


def test_root_line_in_the_wrong_section_is_refused(tmp_path: Path) -> None:
    """A hearings_root under another section would not change the real setting."""
    text = "ccis:\n  hearings_root: x\npaths:\n  logs: logs\n"
    path = _write(tmp_path / "config.yaml", text)

    with pytest.raises(ConfigProblem, match="not saved"):
        save_root_setting(path, "elsewhere")
    assert path.read_text(encoding="utf-8") == text


def test_no_temporary_file_is_left(config_path: Path) -> None:
    """The .new file is renamed over config.yaml, not left beside it."""
    save_root_setting(config_path, "elsewhere")

    assert [p.name for p in config_path.parent.iterdir()] == ["config.yaml"]


# Reading


def test_read_without_a_config_file_says_what_to_do(tmp_path: Path) -> None:
    """A new computer with no config.yaml is told to copy the example."""
    with pytest.raises(ConfigProblem, match="config.example.yaml"):
        read_root_setting(tmp_path / "config.yaml")


def test_read_ignores_the_rest_of_the_file(tmp_path: Path) -> None:
    """Setup can show the folder even when other settings are unfinished."""
    path = _write(tmp_path / "config.yaml", "graph:\n  tenant_id: PASTE-TENANT-ID\npaths:\n  hearings_root: 'x'\n")

    assert read_root_setting(path) == "x"


# Asking


def test_blank_answer_keeps_the_current_folder(tmp_path: Path) -> None:
    """Enter alone changes nothing."""
    assert ask_for_folder(tmp_path, tmp_path, _answers("")) is None


def test_missing_folder_is_asked_again(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A folder that does not exist is refused, then a real one is taken."""
    (tmp_path / "real").mkdir()

    assert ask_for_folder(tmp_path, tmp_path, _answers("typo", "real")) == "real"
    assert "does not exist" in capsys.readouterr().out


def test_quotes_from_copy_as_path_are_removed(tmp_path: Path) -> None:
    """Explorer's "Copy as path" adds double quotes."""
    target = tmp_path / "real"
    target.mkdir()

    assert ask_for_folder(tmp_path, tmp_path, _answers(f'"{target}"')) == str(target)


def test_a_file_is_not_a_folder(tmp_path: Path) -> None:
    """A path to a file is refused like a missing one."""
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    assert ask_for_folder(tmp_path, tmp_path, _answers("file.txt", "")) is None


# The whole exchange


def test_change_drive_saves_the_new_folder(config_path: Path) -> None:
    """--drive with a new folder writes it to config.yaml."""
    (config_path.parent / "moved").mkdir()

    assert change_drive(config_path, config_path.parent, _answers("moved"))
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["paths"]["hearings_root"] == "moved"


def test_confirm_with_enter_continues_unchanged(config_path: Path) -> None:
    """Enter on a double-clicked run asks nothing more and changes nothing."""
    assert not confirm_drive(config_path.parent / "test", config_path, config_path.parent, _answers(""))
    assert config_path.read_text(encoding="utf-8") == CONFIG


def test_confirm_with_c_then_enter_keeps_it(config_path: Path) -> None:
    """Changing your mind after C leaves config.yaml alone."""
    assert not confirm_drive(config_path.parent / "test", config_path, config_path.parent, _answers("c", ""))
    assert config_path.read_text(encoding="utf-8") == CONFIG


def test_confirm_with_c_changes_it(config_path: Path) -> None:
    """C then a real folder saves it."""
    (config_path.parent / "moved").mkdir()

    assert confirm_drive(config_path.parent / "test", config_path, config_path.parent, _answers("C", "moved"))
    assert read_root_setting(config_path) == "moved"
