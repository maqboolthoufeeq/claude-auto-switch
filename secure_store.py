"""Use an explicit encrypted OS credential backend; never fall back to plaintext."""

from __future__ import annotations

import getpass
import sys

from platform_runtime import RotatorError


def backend():
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
        if sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            return Keyring()
    except ImportError as exc:
        raise RotatorError("Secure account storage needs its dependencies. Run uv sync and reopen Switchboard.") from exc
    raise RotatorError("This operating system has no supported secure credential store")


def read(service: str) -> bytes:
    try:
        value = backend().get_password(service, getpass.getuser())
    except RotatorError:
        raise
    except Exception as exc:
        raise RotatorError("Cannot read the OS credential store. Unlock it in your desktop session and retry.") from exc
    if value is None:
        raise RotatorError("No saved credential exists for this account")
    return value.encode("ascii")


def write(service: str, value: bytes) -> None:
    try:
        backend().set_password(service, getpass.getuser(), value.decode("ascii"))
    except RotatorError:
        raise
    except Exception as exc:
        raise RotatorError("Cannot save to the OS credential store. On Linux, install and unlock GNOME Keyring or another Secret Service provider.") from exc
    if read(service) != value:
        raise RotatorError("The OS credential store did not verify the complete saved item")


def delete(service: str) -> None:
    try:
        store = backend()
        if store.get_password(service, getpass.getuser()) is not None:
            store.delete_password(service, getpass.getuser())
    except RotatorError:
        raise
    except Exception as exc:
        raise RotatorError("Cannot remove the saved login from the OS credential store. Unlock it and retry.") from exc
