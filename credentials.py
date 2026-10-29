"""Read and store HRS's secrets in Windows Credential Manager. The only file that does.

config.yaml holds settings anyone may see. The values that grant access live
in Credential Manager instead, under the service HRS:

    HRS / ccis_username         the CCIS login
    HRS / ccis_password         its password
    HRS / graph_client_secret   the Entra app's client secret Value

run_setup() asks for them once per computer. Credential Manager encrypts per
Windows user, so entries stored while signed in as one account cannot be
read by another. A scheduled task running as a different account sees none
of them, which is why errors here name the account that looked.

This is storage, not an access boundary. Anything running as the same
Windows user can read these back exactly as HRS does.
"""

import getpass
import re
from collections.abc import Callable

import keyring
from keyring.errors import KeyringError

from exceptions import ConfigProblem

SERVICE = "HRS"

# Key in Credential Manager -> what it is, for prompts and messages. Setup
# walks this, so adding a secret here is the whole change.
CREDENTIALS = {
    "ccis_username": "CCIS username",
    "ccis_password": "CCIS password",
    "graph_client_secret": "Graph app client secret",
}

# Shown only when asking, where it helps; repeating it on every line is noise.
HINTS = {
    "graph_client_secret": "the Value, not the Secret ID",
}

# Only the username is typed visibly; everything else is hidden as it is typed.
VISIBLE = frozenset({"ccis_username"})

# Backends that would undo the point of keeping secrets out of config.yaml:
# one writes them to a plaintext file, the others store nothing.
REFUSED_BACKENDS = ("PlaintextKeyring", "fail.Keyring", "null.Keyring")


# Copying from a web page often picks up a space or line break at either
# end. Neither belongs in these two; a password may really have them.
STRIPPED = frozenset({"ccis_username", "graph_client_secret"})

# An Entra Secret ID is a GUID. The Value never is, so an answer shaped like
# this is the ID copied from the wrong column.
GUID = re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


class MissingCredentials(ConfigProblem):
    """One or more secrets are not stored for this Windows user."""


def _backend_name() -> str:
    """Return the keyring backend's module and class name."""
    backend = keyring.get_keyring()
    return f"{type(backend).__module__}.{type(backend).__qualname__}"


def _check_backend() -> None:
    """Refuse a keyring backend that stores nothing or stores in plaintext."""
    name = _backend_name()
    if any(refused in name for refused in REFUSED_BACKENDS):
        raise ConfigProblem(
            f"keyring is using {name}, which stores nothing or stores secrets in "
            "a plaintext file. HRS expects Windows Credential Manager."
        )


def _read(key: str) -> str | None:
    """Return one stored secret, or None if it is missing or blank."""
    try:
        value = keyring.get_password(SERVICE, key)
    except KeyringError as error:
        raise ConfigProblem(
            f"Could not read {SERVICE}/{key} from Credential Manager: {error}"
        ) from error
    return value if value and value.strip() else None


def load_credentials() -> dict[str, str]:
    """Return every HRS secret, keyed as in CREDENTIALS.

    Every key is checked before raising, so a computer missing two secrets is
    told about both at once. Values come back exactly as stored.

    Raises:
        MissingCredentials: If any secret is missing or blank.
        ConfigProblem: If the keyring backend is unusable or unsafe.
    """
    _check_backend()
    found = {key: _read(key) for key in CREDENTIALS}
    missing = [key for key, value in found.items() if value is None]
    if missing:
        user = getpass.getuser()
        raise MissingCredentials(
            f"Credential Manager has no {', '.join(f'{SERVICE}/{k}' for k in missing)} "
            f"for Windows user '{user}'. Run HRS with --setup while signed in as "
            f"'{user}'. Secrets stored under another account are invisible to this one."
        )
    return found  # type: ignore[return-value]


def _entry_problem(key: str, value: str) -> str | None:
    """Return why a typed secret cannot be right, or None if it may be.

    The message names the mistake, never the value.
    """
    if not value.strip():
        return "Nothing was entered. Try again."
    # At a hidden prompt in the classic console, Ctrl+V does not paste: it
    # enters the invisible character 0x16 instead.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ("That contained an invisible control character, usually Ctrl+V "
                "not pasting. Paste with a right-click instead, then press Enter.")
    if key == "graph_client_secret" and GUID.fullmatch(value):
        return ("That is the Secret ID. Paste the secret's Value, from the Value "
                "column, not the Secret ID column.")
    return None


def run_setup(
    ask: Callable[[str], str] = input,
    ask_hidden: Callable[[str], str] = getpass.getpass,
) -> None:
    """Ask for each secret and store it in Credential Manager.

    A secret already stored is kept unless the person answers y to replace
    it. An answer that cannot be right (see _entry_problem) stores nothing
    and asks again, since it would only fail later at sign-in, out of
    sight. No value is ever printed.

    Raises:
        ConfigProblem: If the keyring backend is unsafe or a write fails.
    """
    _check_backend()
    print(f"Storing HRS secrets for Windows user '{getpass.getuser()}'.")
    for key, label in CREDENTIALS.items():
        if _read(key) is not None:
            if ask(f"{label} is already stored. Replace it? [y/N] ").strip().lower() != "y":
                continue
        prompt = ask if key in VISIBLE else ask_hidden
        hint = f" ({HINTS[key]})" if key in HINTS else ""
        while True:
            value = prompt(f"{label}{hint}: ")
            if key in STRIPPED:
                value = value.strip()
            problem = _entry_problem(key, value)
            if problem is None:
                break
            print(f"  {problem}")
        try:
            keyring.set_password(SERVICE, key, value)
        except KeyringError as error:
            raise ConfigProblem(
                f"Could not store {SERVICE}/{key} in Credential Manager: {error}"
            ) from error
        print(f"  {label} stored.")
    print("Setup complete.")
