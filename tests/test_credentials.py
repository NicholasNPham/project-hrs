"""Tests for credentials against a fake keyring. Credential Manager is never touched."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import credentials  # noqa: E402
from credentials import CREDENTIALS, SERVICE, MissingCredentials  # noqa: E402
from exceptions import ConfigProblem  # noqa: E402


class FakeKeyring:
    """A dict standing in for Credential Manager."""

    def __init__(self, stored: dict[str, str] | None = None) -> None:
        """Start with these keys stored under SERVICE."""
        self.stored = {(SERVICE, key): value for key, value in (stored or {}).items()}

    def get_password(self, service: str, key: str) -> str | None:
        """Return a stored value or None."""
        return self.stored.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        """Store a value."""
        self.stored[(service, key)] = value


class WinVaultKeyring:
    """Named like the real Windows backend so the backend check passes."""


class PlaintextKeyring:
    """Named like the plaintext backend the check refuses."""


ALL = {key: f"fake-{key}" for key in CREDENTIALS}


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeKeyring, backend: type = WinVaultKeyring) -> None:
    """Point credentials at the fake keyring and a named backend."""
    monkeypatch.setattr(credentials.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(credentials.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(credentials.keyring, "get_keyring", backend)
    monkeypatch.setattr(credentials.getpass, "getuser", lambda: "test-user")


def _answers(*values: str):
    """A prompt function that gives these answers in order."""
    queue = list(values)
    return lambda prompt: queue.pop(0)


# load_credentials


def test_all_stored_returns_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every secret comes back exactly as stored."""
    _use(monkeypatch, FakeKeyring({**ALL, "ccis_password": " spaced "}))

    assert credentials.load_credentials() == {**ALL, "ccis_password": " spaced "}


def test_every_missing_key_is_named_with_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two missing secrets are reported together, with the account that looked."""
    _use(monkeypatch, FakeKeyring({"ccis_username": "fake-user"}))

    with pytest.raises(MissingCredentials) as caught:
        credentials.load_credentials()
    message = str(caught.value)
    assert "HRS/ccis_password" in message
    assert "HRS/graph_client_secret" in message
    assert "HRS/ccis_username" not in message
    assert "test-user" in message


def test_blank_secret_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret of only spaces would fail at sign-in, so it is reported now."""
    _use(monkeypatch, FakeKeyring({**ALL, "graph_client_secret": "   "}))

    with pytest.raises(MissingCredentials, match="graph_client_secret"):
        credentials.load_credentials()


def test_missing_credentials_is_a_config_problem() -> None:
    """Callers that stop on ConfigProblem also stop on missing secrets."""
    assert issubclass(MissingCredentials, ConfigProblem)


def test_plaintext_backend_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that writes secrets to a file is refused before any read."""
    _use(monkeypatch, FakeKeyring(ALL), backend=PlaintextKeyring)

    with pytest.raises(ConfigProblem, match="plaintext"):
        credentials.load_credentials()


def test_values_never_appear_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial store does not echo the secrets it did find."""
    _use(monkeypatch, FakeKeyring({"ccis_username": "fake-user", "ccis_password": "fake-pass"}))

    with pytest.raises(MissingCredentials) as caught:
        credentials.load_credentials()
    assert "fake-user" not in str(caught.value)
    assert "fake-pass" not in str(caught.value)


# run_setup


def test_setup_on_a_new_computer_stores_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing stored, each secret is asked for once and stored."""
    fake = FakeKeyring()
    _use(monkeypatch, fake)

    credentials.run_setup(ask=_answers("fake-user"), ask_hidden=_answers("fake-pass", "fake-secret"))

    assert credentials.load_credentials() == {
        "ccis_username": "fake-user",
        "ccis_password": "fake-pass",
        "graph_client_secret": "fake-secret",
    }


def test_setup_keeps_stored_secrets_unless_told(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter keeps a stored secret; y replaces it."""
    fake = FakeKeyring(ALL)
    _use(monkeypatch, fake)

    credentials.run_setup(ask=_answers("", "y", "n"), ask_hidden=_answers("new-pass"))

    assert credentials.load_credentials() == {**ALL, "ccis_password": "new-pass"}


def test_setup_asks_again_after_a_blank_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank secret is never stored."""
    fake = FakeKeyring({key: value for key, value in ALL.items() if key != "graph_client_secret"})
    _use(monkeypatch, fake)

    credentials.run_setup(ask=_answers("", ""), ask_hidden=_answers("", "  ", "fake-secret"))

    assert credentials.load_credentials()["graph_client_secret"] == "fake-secret"


def test_setup_never_prints_a_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Prompts and confirmations name the secret, never its value."""
    _use(monkeypatch, FakeKeyring())

    credentials.run_setup(ask=_answers("fake-user"), ask_hidden=_answers("fake-pass", "fake-secret"))

    out = capsys.readouterr().out
    for value in ("fake-user", "fake-pass", "fake-secret"):
        assert value not in out


def test_setup_hides_everything_but_the_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the username goes through the visible prompt."""
    _use(monkeypatch, FakeKeyring())
    visible: list[str] = []
    hidden: list[str] = []

    def ask(prompt: str) -> str:
        visible.append(prompt)
        return "fake"

    def ask_hidden(prompt: str) -> str:
        hidden.append(prompt)
        return "fake"

    credentials.run_setup(ask=ask, ask_hidden=ask_hidden)

    assert visible == ["CCIS username: "]
    assert len(hidden) == 2


def test_hint_is_shown_when_asking_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The Secret ID warning is on the prompt, not repeated on the stored line."""
    _use(monkeypatch, FakeKeyring())
    hidden: list[str] = []

    def ask_hidden(prompt: str) -> str:
        hidden.append(prompt)
        return "fake"

    credentials.run_setup(ask=_answers("fake-user"), ask_hidden=ask_hidden)

    assert hidden[-1] == "Graph app client secret (the Value, not the Secret ID): "
    assert "  Graph app client secret stored." in capsys.readouterr().out


def _graph_secret_only(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """Everything stored but the Graph secret, so setup asks for it alone."""
    fake = FakeKeyring({key: value for key, value in ALL.items() if key != "graph_client_secret"})
    _use(monkeypatch, fake)
    return fake


def test_ctrl_v_that_did_not_paste_is_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The 0x16 a failed Ctrl+V leaves is caught, and the next answer is taken."""
    _graph_secret_only(monkeypatch)

    credentials.run_setup(ask=_answers("", ""), ask_hidden=_answers("\x16", "fake-secret"))

    assert credentials.load_credentials()["graph_client_secret"] == "fake-secret"
    assert "right-click" in capsys.readouterr().out


def test_secret_id_is_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A GUID is the Secret ID, never the Value."""
    _graph_secret_only(monkeypatch)
    secret_id = "00000000-1111-2222-3333-444444444444"

    credentials.run_setup(ask=_answers("", ""), ask_hidden=_answers(secret_id, "fake-secret"))

    assert credentials.load_credentials()["graph_client_secret"] == "fake-secret"
    out = capsys.readouterr().out
    assert "Secret ID" in out
    assert secret_id not in out


def test_graph_secret_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A space or line break picked up when copying is dropped."""
    _graph_secret_only(monkeypatch)

    credentials.run_setup(ask=_answers("", ""), ask_hidden=_answers("  fake-secret \n"))

    assert credentials.load_credentials()["graph_client_secret"] == "fake-secret"


def test_password_is_not_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password may really start or end with a space."""
    fake = FakeKeyring({key: value for key, value in ALL.items() if key != "ccis_password"})
    _use(monkeypatch, fake)

    credentials.run_setup(ask=_answers("", ""), ask_hidden=_answers(" fake pass "))

    assert credentials.load_credentials()["ccis_password"] == " fake pass "
