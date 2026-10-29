"""Tests for how main decides whether a person is at the screen, and for --setup.

load_config, setup and the drive prompt are replaced with fakes, so nothing
here reads config.yaml, Credential Manager, Graph or a drive.
"""

import io
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from credentials import MissingCredentials  # noqa: E402
from exceptions import ConfigProblem, SystemProblem  # noqa: E402


class Calls:
    """Counts calls to the fakes."""

    def __init__(self) -> None:
        """Start every count at zero."""
        self.loads = 0
        self.setups = 0
        self.confirms = 0
        self.drives = 0
        self.runs: list[tuple] = []
        self.pauses = 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Calls:
    """Fake every outside step main uses here."""
    state = Calls()
    config = {"ccis": {"headless": False}, "paths": {"hearings_root": tmp_path, "logs": tmp_path}}

    def load() -> dict:
        state.loads += 1
        return config

    def setup() -> None:
        state.setups += 1

    def confirm(root: Path, config_path: Path, app_root: Path) -> bool:
        state.confirms += 1
        return False

    def drive(config_path: Path, app_root: Path) -> bool:
        state.drives += 1
        return False

    def run(path, limit, force, interactive, headless) -> int:
        state.runs.append((path, limit, force, interactive, headless))
        return 0

    monkeypatch.setattr(main, "load_config", load)
    monkeypatch.setattr(main, "run_setup", setup)
    monkeypatch.setattr(main, "confirm_drive", confirm)
    monkeypatch.setattr(main, "change_drive", drive)
    monkeypatch.setattr(main, "setup_logging", lambda path: None)
    monkeypatch.setattr(main, "run", run)
    monkeypatch.setattr(main, "pause_before_closing", lambda: setattr(state, "pauses", state.pauses + 1))
    return state


def _missing_once(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    """Make the first load_config report missing secrets, later ones succeed."""
    real = main.load_config

    def load() -> dict:
        if calls.loads == 0:
            calls.loads += 1
            raise MissingCredentials("none stored")
        return real()

    monkeypatch.setattr(main, "load_config", load)


def _console(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    """Pretend stdin is, or is not, a console someone can type into."""
    stdin = io.StringIO()
    stdin.isatty = lambda: is_tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)


# start()


def test_scheduled_start_with_missing_secrets_stops(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a person there, missing secrets stop the run instead of prompting."""
    _missing_once(monkeypatch, calls)

    with pytest.raises(MissingCredentials):
        main.start(interactive=False)
    assert calls.setups == 0


def test_interactive_start_with_missing_secrets_runs_setup(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a person there, setup runs and the config is loaded again."""
    _missing_once(monkeypatch, calls)

    main.start(interactive=True)
    assert calls.setups == 1
    assert calls.loads == 2


def test_interactive_start_confirms_the_drive(calls: Calls) -> None:
    """A double-clicked run shows the folder once."""
    main.start(interactive=True)
    assert calls.confirms == 1
    assert calls.loads == 1


def test_changed_drive_reloads_the_config(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a change, the run uses the new folder."""
    monkeypatch.setattr(main, "confirm_drive", lambda *args: True)

    main.start(interactive=True)
    assert calls.loads == 2


def test_scheduled_start_never_asks(calls: Calls) -> None:
    """No drive prompt when nobody is there."""
    main.start(interactive=False)
    assert calls.confirms == 0


# main()


def test_no_arguments_is_an_interactive_email_run(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """A double-click runs today's email with prompts."""
    _console(monkeypatch, is_tty=True)

    assert main.main([]) == 0
    assert calls.runs == [(None, None, False, True, None)]


def test_no_arguments_without_a_console_does_not_ask(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """With stdin redirected there is nobody to answer."""
    _console(monkeypatch, is_tty=False)

    main.main([])
    assert calls.runs == [(None, None, False, False, None)]


def test_email_argument_never_asks_even_on_a_console(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task Scheduler can give --email a console; it still must not prompt."""
    _console(monkeypatch, is_tty=True)

    main.main(["--email"])
    assert calls.runs == [(None, None, False, False, None)]


@pytest.mark.parametrize("argv", [["--setup", "--email"], ["--drive", "--limit", "3"], ["--setup", "--drive"]])
def test_setup_and_drive_stand_alone(calls: Calls, argv: list[str]) -> None:
    """Mixing them with a run is refused before anything happens."""
    assert main.main(argv) == 2
    assert calls.setups == calls.drives == 0
    assert calls.runs == []


def test_drive_argument_only_changes_the_drive(calls: Calls) -> None:
    """--drive asks about the folder and does not run."""
    assert main.main(["--drive"]) == 0
    assert calls.drives == 1
    assert calls.runs == []


# --setup


def test_setup_reports_complete(calls: Calls) -> None:
    """Secrets, then the drive, then a full load that succeeds."""
    assert main.main(["--setup"]) == 0
    assert (calls.setups, calls.drives, calls.loads) == (1, 1, 1)


def test_setup_reports_what_is_left(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """An unfinished config.yaml is named, and the exit code says so."""
    def load() -> dict:
        raise ConfigProblem("graph.tenant_id is not filled in in config.yaml.")

    monkeypatch.setattr(main, "load_config", load)

    assert main.main(["--setup"]) == 1
    assert "Still to fix: graph.tenant_id" in capsys.readouterr().out


def test_setup_carries_on_without_a_config_file(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """On a new computer the secrets are still stored before the missing file is reported."""
    def drive(config_path: Path, app_root: Path) -> bool:
        raise ConfigProblem("No config.yaml yet.")

    monkeypatch.setattr(main, "change_drive", drive)

    main.main(["--setup"])
    assert calls.setups == 1
    assert "Drive folder not set: No config.yaml yet." in capsys.readouterr().out


def test_ctrl_c_during_setup_cancels_cleanly(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """No traceback when someone backs out."""
    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "run_setup", interrupted)

    assert main.main(["--setup"]) == 1
    assert "Cancelled." in capsys.readouterr().out


# --headless


@pytest.mark.parametrize("headless", [True, False])
def test_headless_override_replaces_the_config_value(calls: Calls, headless: bool) -> None:
    """Either direction wins over config.yaml for this run."""
    config, _ = main.start(headless=headless)
    assert config["ccis"]["headless"] is headless


def test_no_override_keeps_the_config_value(calls: Calls) -> None:
    """Without the flag, config.yaml decides."""
    config, _ = main.start()
    assert config["ccis"]["headless"] is False


def test_override_survives_a_reload_after_a_drive_change(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reloading config.yaml after C does not undo the flag."""
    monkeypatch.setattr(main, "confirm_drive", lambda *args: True)

    config, _ = main.start(interactive=True, headless=True)
    assert config["ccis"]["headless"] is True


@pytest.mark.parametrize(("flag", "expected"), [("--headless", True), ("--no-headless", False)])
def test_headless_flags_reach_the_run(calls: Calls, flag: str, expected: bool) -> None:
    """The command line passes the override through."""
    main.main(["--email", flag])
    assert calls.runs == [(None, None, False, False, expected)]


def test_headless_cannot_join_setup(calls: Calls) -> None:
    """--setup stands alone, with no run to apply the flag to."""
    assert main.main(["--setup", "--headless"]) == 2


# Press Enter to close


def test_double_click_waits_before_closing(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """The window stays open after the summary."""
    _console(monkeypatch, is_tty=True)

    main.main([])
    assert calls.pauses == 1


def test_scheduled_run_never_waits(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """--email on a console must not hang at the end."""
    _console(monkeypatch, is_tty=True)

    main.main(["--email"])
    assert calls.pauses == 0


def test_double_click_waits_after_a_stop(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A stopped run is the one most worth reading before the window closes."""
    _console(monkeypatch, is_tty=True)

    def stopped(*args) -> int:
        raise SystemProblem("No booking email arrived by 09:00.")

    monkeypatch.setattr(main, "run", stopped)

    assert main.main([]) == 1
    assert "Stopped: No booking email" in capsys.readouterr().out
    assert calls.pauses == 1


def test_double_click_shows_an_unexpected_error_and_waits(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A bug's traceback stays on screen instead of flashing past."""
    _console(monkeypatch, is_tty=True)

    def broken(*args) -> int:
        raise ValueError("a bug")

    monkeypatch.setattr(main, "run", broken)

    assert main.main([]) == 1
    assert "ValueError: a bug" in capsys.readouterr().err
    assert calls.pauses == 1


def test_scheduled_run_lets_an_unexpected_error_through(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task Scheduler sees the crash as a failure."""
    def broken(*args) -> int:
        raise ValueError("a bug")

    monkeypatch.setattr(main, "run", broken)

    with pytest.raises(ValueError):
        main.main(["--email"])
    assert calls.pauses == 0


def test_pause_survives_closed_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """No input to read ends the pause rather than crashing."""
    _console(monkeypatch, is_tty=True)

    main.pause_before_closing()


# What reaches the log


class Recorder(logging.Handler):
    """Keeps every record it is given."""

    def __init__(self) -> None:
        """Start empty."""
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the formatted message."""
        self.lines.append(record.getMessage())


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A logger with a handler, standing in for a run whose log has started."""
    recorder = Recorder()
    logger = logging.getLogger("HRS-test-stops")
    logger.handlers = [recorder]
    logger.propagate = False
    monkeypatch.setattr(main, "get_logger", lambda: logger)
    return recorder


def test_a_stop_reaches_the_log(calls: Calls, monkeypatch: pytest.MonkeyPatch, recorded: Recorder) -> None:
    """A scheduled run's reason for stopping is kept, not just printed."""
    def stopped(*args) -> int:
        raise SystemProblem("No booking email arrived by 08:00. Nothing was searched.")

    monkeypatch.setattr(main, "run", stopped)

    assert main.main(["--email"]) == 1
    assert recorded.lines == ["Stopped: No booking email arrived by 08:00. Nothing was searched."]


def test_an_unexpected_error_logs_its_type_and_place_only(calls: Calls, monkeypatch: pytest.MonkeyPatch, recorded: Recorder) -> None:
    """The message of a surprise error is left out of the log."""
    def broken(*args) -> int:
        raise ValueError("DOE, JOHN 01/01/1900")

    monkeypatch.setattr(main, "run", broken)

    with pytest.raises(ValueError):
        main.main(["--email"])
    assert len(recorded.lines) == 1
    assert recorded.lines[0].startswith("Stopped on an unexpected ValueError at test_main_modes.py:")
    assert "DOE" not in recorded.lines[0]


def test_a_stop_before_the_log_starts_is_printed(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """With no log yet, the console is the only place to say it."""
    bare = logging.getLogger("HRS-test-no-handlers")
    bare.handlers = []
    monkeypatch.setattr(main, "get_logger", lambda: bare)

    def stopped(*args) -> int:
        raise SystemProblem("config.yaml has no 'graph' section.")

    monkeypatch.setattr(main, "run", stopped)

    main.main(["--email"])
    assert "Stopped: config.yaml has no 'graph' section." in capsys.readouterr().out
