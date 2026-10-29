"""Tests for main.wait_for_email with a fake clock. No waiting, no Graph."""

import sys
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exceptions import SystemProblem  # noqa: E402
from graph_client import EmailNotArrived  # noqa: E402
from main import wait_for_email  # noqa: E402

TODAY = date(2026, 1, 5)
UNTIL = clock(9, 0)
EVERY = timedelta(minutes=5)
SHEET = b"fake xlsx bytes"


class FakeClock:
    """A clock that only moves when sleep is called."""

    def __init__(self, start: clock) -> None:
        """Start at this time on TODAY."""
        self.current = datetime.combine(TODAY, start)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        """Return the current fake time."""
        return self.current

    def sleep(self, seconds: float) -> None:
        """Move the clock forward instead of waiting."""
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeFetch:
    """Reports not arrived until a set time, then returns the sheet."""

    def __init__(self, fake_clock: FakeClock, arrives: clock | None) -> None:
        """arrives None means the email never comes."""
        self.clock = fake_clock
        self.arrives = datetime.combine(TODAY, arrives) if arrives else None
        self.checks: list[datetime] = []

    def __call__(self) -> bytes:
        """One check of the Inbox."""
        self.checks.append(self.clock.now())
        if self.arrives is None or self.clock.now() < self.arrives:
            raise EmailNotArrived("not yet")
        return SHEET


def _wait(fake_clock: FakeClock, fetch) -> bytes:
    """Run wait_for_email with the fakes and the test settings."""
    return wait_for_email(fetch, TODAY, UNTIL, EVERY, now=fake_clock.now, sleep=fake_clock.sleep)


def _at(hour: int, minute: int) -> datetime:
    """A time on TODAY."""
    return datetime.combine(TODAY, clock(hour, minute))


def test_email_already_there_returns_without_waiting() -> None:
    """The first check finds it."""
    fake_clock = FakeClock(clock(5, 30))
    fetch = FakeFetch(fake_clock, arrives=clock(5, 0))

    assert _wait(fake_clock, fetch) == SHEET
    assert fake_clock.sleeps == []


def test_checks_every_interval_until_it_arrives() -> None:
    """Starting 5:30 with the email at 5:42, checks land at 5:30, 5:35, 5:40, 5:45."""
    fake_clock = FakeClock(clock(5, 30))
    fetch = FakeFetch(fake_clock, arrives=clock(5, 42))

    assert _wait(fake_clock, fetch) == SHEET
    assert fetch.checks == [_at(5, 30), _at(5, 35), _at(5, 40), _at(5, 45)]


def test_never_arrives_stops_at_the_deadline() -> None:
    """The last check is at 9:00 exactly, then the run stops."""
    fake_clock = FakeClock(clock(5, 30))
    fetch = FakeFetch(fake_clock, arrives=None)

    with pytest.raises(SystemProblem, match="No booking email arrived by 09:00") as caught:
        _wait(fake_clock, fetch)
    assert not isinstance(caught.value, EmailNotArrived)
    assert fetch.checks[-1] == _at(9, 0)
    assert len(fetch.checks) == 43  # 5:30 to 9:00 every 5 minutes, both ends


def test_last_wait_is_shortened_to_hit_the_deadline() -> None:
    """From 8:57 a full 5 minutes would overrun, so it checks again at 9:00."""
    fake_clock = FakeClock(clock(8, 57))
    fetch = FakeFetch(fake_clock, arrives=clock(8, 59))

    assert _wait(fake_clock, fetch) == SHEET
    assert fetch.checks == [_at(8, 57), _at(9, 0)]


def test_started_after_the_deadline_checks_once() -> None:
    """A double-click at 10:00 looks once and does not wait."""
    fake_clock = FakeClock(clock(10, 0))
    fetch = FakeFetch(fake_clock, arrives=None)

    with pytest.raises(SystemProblem, match="09:00"):
        _wait(fake_clock, fetch)
    assert fetch.checks == [_at(10, 0)]
    assert fake_clock.sleeps == []


def test_started_after_the_deadline_still_finds_the_email() -> None:
    """Late is fine if the email is there."""
    fake_clock = FakeClock(clock(10, 0))
    fetch = FakeFetch(fake_clock, arrives=clock(6, 0))

    assert _wait(fake_clock, fetch) == SHEET


def test_other_problems_stop_at_once() -> None:
    """Two emails, a bad attachment or a Graph error is not waited out."""
    fake_clock = FakeClock(clock(5, 30))
    checks: list[datetime] = []

    def fetch() -> bytes:
        checks.append(fake_clock.now())
        raise SystemProblem("Expected one booking email, found 2.")

    with pytest.raises(SystemProblem, match="found 2"):
        _wait(fake_clock, fetch)
    assert checks == [_at(5, 30)]
    assert fake_clock.sleeps == []


def test_deadline_message_keeps_the_last_reason() -> None:
    """At the deadline, the reason from the last check is passed on."""
    fake_clock = FakeClock(clock(10, 0))

    def fetch() -> bytes:
        raise EmailNotArrived("No email from graph.sender in the Inbox on 01/05/2026.")

    with pytest.raises(SystemProblem, match="Nothing was searched. No email from graph.sender"):
        _wait(fake_clock, fetch)
