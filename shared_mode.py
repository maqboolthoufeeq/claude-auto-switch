#!/usr/bin/env python3
"""Rotate the Claude login shared by independent local terminal sessions."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

from rotator import (
    BASE_DIR, DATA_DIR, Account, Config, RotatorError, atomic_json,
    child_env, claude_binary, load_config, usage_percent,
)
from session_usage import read_session_usage
from platform_runtime import (file_lock, interrupt_process, other_install_locks, popen_detached, private_directory,
                              windows_monitor_job)
import secure_store
import background_monitor
from platform_runtime import check_macos_version


STATE_DIR = DATA_DIR / ".shared-rotator"
STATE_FILE = STATE_DIR / "state.json"
HOOK_MARKER = "# switchboard-shared-login"
COOLDOWN_SECONDS = 5 * 60 * 60
# Open chats keep sending with the previous login's cached token for a while after a switch, and a workflow's
# agents report those failures late. Limit reports this soon after a switch are about the previous account.
LIMIT_REPORT_GRACE = 90
NO_ACCOUNT_ERROR = "No prepared account is available. Sign in again to accounts with login errors, or wait for session usage to reset."
MONITOR_ERROR = "The session usage monitor could not start."
NOT_SIGNED_IN = "Claude is not signed in with a claude.ai subscription"


def use_config(config_path: Path) -> None:
    """Keep state beside the config a command was given: monitor and hook commands carry --config, not CSWITCH_HOME."""
    global STATE_DIR, STATE_FILE
    STATE_DIR = config_path.parent / ".shared-rotator"
    STATE_FILE = STATE_DIR / "state.json"


def require_macos() -> None:
    if sys.platform != "darwin":
        raise RotatorError("This Keychain operation requires macOS")
    check_macos_version()


def base_env() -> dict[str, str]:
    env = child_env()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def shared_env() -> dict[str, str]:
    env = base_env()
    # Independent terminals started with plain `claude` use this same default root.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDECODE", None)
    return env


def keychain_service(config_dir: Path | None) -> str:
    if config_dir is None:
        return "Claude Code-credentials"
    suffix = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{suffix}"


def vault_service(config_path: Path, account: Account) -> str:
    scope = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]
    return f"Switchboard-v4-{scope}-{account.name}"


def previous_vault_service(config_path: Path, account: Account) -> str:
    scope = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]
    return f"Switchboard-v3-{scope}-{account.name}"


def old_vault_service(config_path: Path, account: Account) -> str:
    scope = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]
    return f"Switchboard-v2-{scope}-{account.name}"


def keychain_read(service: str) -> str:
    require_macos()
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RotatorError("Keychain access timed out. Unlock your Keychain and try again.") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RotatorError(f"No readable Keychain credential for {service}")
    return proc.stdout.rstrip("\n")


def claude_credential(config_dir: Path | None) -> str:
    if sys.platform != "darwin":
        folder = config_dir if config_dir is not None else Path.home() / ".claude"
        try:
            return (folder / ".credentials.json").read_text(encoding="utf-8")
        except OSError as exc:
            raise RotatorError("Claude's saved login cannot be read. Sign in through Claude on this computer first.") from exc
    try:
        return keychain_read(keychain_service(config_dir))
    except RotatorError as keychain_error:
        folder = config_dir if config_dir is not None else Path.home() / ".claude"
        fallback = folder / ".credentials.json"
        try:
            return fallback.read_text(encoding="utf-8")
        except OSError:
            raise keychain_error


def keychain_helper_binary() -> Path:
    require_macos()
    source = BASE_DIR / "keychain_writer.swift"
    binary = STATE_DIR / "keychain-helper"
    if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    compiler = shutil.which("swiftc")
    if not compiler:
        raise RotatorError("Swift compiler is required for macOS Keychain storage")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    module_cache = STATE_DIR / "swift-module-cache"
    clang_cache = STATE_DIR / "clang-module-cache"
    module_cache.mkdir(exist_ok=True)
    clang_cache.mkdir(exist_ok=True)
    temp = binary.with_suffix(".tmp")
    env = base_env()
    env["SWIFT_MODULE_CACHE_PATH"] = str(module_cache)
    env["CLANG_MODULE_CACHE_PATH"] = str(clang_cache)
    try:
        proc = subprocess.run([compiler, "-module-cache-path", str(module_cache),
                               str(source), "-o", str(temp)], env=env,
                              capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError(f"Could not build Keychain helper: {exc}") from exc
    if proc.returncode != 0:
        raise RotatorError("Could not build Keychain helper; install Xcode command line tools")
    os.chmod(temp, 0o700)
    os.replace(temp, binary)
    return binary


def _keychain_item_read(service: str) -> bytes:
    if sys.platform != "darwin":
        return secure_store.read(service)
    try:
        proc = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                               "-s", service, "-w"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError("Keychain access failed or timed out; unlock your Keychain and retry") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise RotatorError(f"No readable Switchboard Keychain credential for {service}")
    return proc.stdout.rstrip(b"\n")


def _keychain_item_write(service: str, value: bytes) -> None:
    if sys.platform != "darwin":
        secure_store.write(service, value)
        return
    # security's password prompt truncates at 128 characters; its command parser also has a line limit.
    command = shlex.join(["add-generic-password", "-a", getpass.getuser(), "-s", service,
                          "-U", "-w", value.decode("ascii")]) + "\n"
    if len(command.encode()) > 900:
        raise RotatorError("The saved login's service name is too long for Keychain storage")
    try:
        proc = subprocess.run(["/usr/bin/security", "-i"], input=command.encode(),
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError("Keychain storage failed or timed out; unlock your Keychain and retry") from exc
    if proc.returncode != 0 or _keychain_item_read(service) != value:
        raise RotatorError("Keychain did not verify the complete saved item")


def _vault_manifest(service: str) -> dict[str, Any]:
    try:
        value = json.loads(base64.b64decode(_keychain_item_read(service), validate=True))
        if (not isinstance(value, dict) or value.get("v") != 4
                or not isinstance(value.get("n"), int) or isinstance(value["n"], bool)
                or not 1 <= value["n"] <= 1024
                or not isinstance(value.get("g"), str) or len(value["g"]) != 16
                or any(c not in "0123456789abcdef" for c in value["g"])
                or not isinstance(value.get("h"), str) or len(value["h"]) != 64):
            raise ValueError("Invalid saved credential manifest")
        return value
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise RotatorError("The saved credential index is damaged; prepare accounts again") from exc


def vault_read(service: str) -> str:
    value = _vault_manifest(service)
    encoded = b"".join(_keychain_item_read(f"{service}-{value['g']}-{i}") for i in range(value["n"]))
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), value["h"]):
            raise ValueError("Saved credential checksum mismatch")
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise RotatorError("The saved credential is incomplete; prepare accounts again") from exc


def old_vault_read(service: str) -> str:
    binary = keychain_helper_binary()
    proc = subprocess.run([str(binary), "read", service], capture_output=True,
                          timeout=30, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RotatorError(f"Could not read the older saved credential for {service}")
    return proc.stdout.decode("utf-8")


def _keychain_item_delete(service: str) -> None:
    if sys.platform != "darwin":
        secure_store.delete(service)
        return
    try:
        proc = subprocess.run(["/usr/bin/security", "delete-generic-password", "-a", getpass.getuser(),
                               "-s", service], capture_output=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError("Keychain removal failed or timed out; unlock your Keychain and retry") from exc
    if proc.returncode not in (0, 44):
        raise RotatorError(f"Could not remove saved Keychain credential for {service}")


def keychain_write(service: str, secret: str) -> None:
    encoded = base64.b64encode(secret.encode("utf-8"))
    if not encoded or len(encoded) > 512 * 1024:
        raise RotatorError("Credential size is outside the supported range")
    try:
        previous = _vault_manifest(service)
    except RotatorError:
        previous = None
    generation = secrets.token_hex(8)
    chunks = [encoded[i:i + 512] for i in range(0, len(encoded), 512)]
    written = []
    committed = False
    root_attempted = False
    try:
        for i, chunk in enumerate(chunks):
            name = f"{service}-{generation}-{i}"
            written.append(name)
            _keychain_item_write(name, chunk)
        manifest = {"v": 4, "g": generation, "n": len(chunks),
                    "h": hashlib.sha256(secret.encode("utf-8")).hexdigest()}
        root_attempted = True
        _keychain_item_write(service, base64.b64encode(json.dumps(manifest).encode()))
        committed = True
    except BaseException:  # also Ctrl+C: never leave a manifest pointing at chunks the finally block deletes
        if root_attempted:
            try:
                if previous:
                    _keychain_item_write(service, base64.b64encode(json.dumps(previous).encode()))
                else:
                    _keychain_item_delete(service)
            except RotatorError:
                # Keep both generations if access is interrupted during rollback.
                committed = True
        raise
    finally:
        if not committed:
            for name in written:
                try:
                    _keychain_item_delete(name)
                except RotatorError:
                    pass
    if previous:
        for i in range(previous["n"]):
            try:
                _keychain_item_delete(f"{service}-{previous['g']}-{i}")
            except RotatorError:
                pass


def vault_delete(service: str) -> None:
    if service.startswith("Switchboard-v4-"):
        try:
            value = _vault_manifest(service)
        except RotatorError:
            # Missing items are already deleted; damaged existing items require repair.
            try:
                _keychain_item_read(service)
            except RotatorError:
                return
            raise
        for i in range(value["n"]):
            _keychain_item_delete(f"{service}-{value['g']}-{i}")
    _keychain_item_delete(service)


def migrate_vault(config: Config, config_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Move older per-account items to items owned by Apple's signed security tool."""
    prepared = [account for account in config.accounts if account.name in state.get("identities", {})]
    if state.get("vault_version") != 4:
        for account in prepared:
            service = vault_service(config_path, account)
            try:
                credential_parts(vault_read(service))
                continue
            except RotatorError:
                pass
            if account.name == state.get("current"):
                if auth_identity(claude_binary(config), shared_env()) != state["identities"][account.name]:
                    raise RotatorError("The shared login changed outside Switchboard; prepare accounts again")
                raw = claude_credential(None)
            else:
                try:
                    raw = base64.b64decode(_keychain_item_read(previous_vault_service(config_path, account)), validate=True).decode()
                    credential_parts(raw)
                except (RotatorError, ValueError, UnicodeDecodeError, binascii.Error):
                    try:
                        raw = old_vault_read(old_vault_service(config_path, account))
                        credential_parts(raw)
                    except RotatorError:
                        env = base_env()
                        env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
                        if auth_identity(claude_binary(config), env) != state["identities"][account.name]:
                            raise RotatorError(f"Sign in to {account.display_name} again before repairing its saved login")
                        raw = claude_credential(account.config_dir)
            credential_parts(raw)
            keychain_write(service, raw)
        state["vault_version"] = 4
    cleanup_pending = []
    for account in prepared:
        try:
            vault_delete(old_vault_service(config_path, account))
            vault_delete(previous_vault_service(config_path, account))
        except RotatorError:
            cleanup_pending.append(account.name)
    state["old_vault_cleanup_pending"] = cleanup_pending
    state_save(state)
    return state


def credential_parts(raw: str) -> tuple[str, str]:
    try:
        value = json.loads(raw)
        oauth = value["claudeAiOauth"]
        refresh = oauth["refreshToken"]
        scopes = oauth.get("scopes", [])
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("missing refresh token")
        if isinstance(scopes, list) and scopes and all(isinstance(s, str) and s for s in scopes):
            scope_text = " ".join(scopes)
        elif isinstance(scopes, str) and scopes.strip():
            scope_text = scopes.strip()
        else:
            raise ValueError("missing OAuth scopes")
        return refresh, scope_text
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RotatorError("Claude credential has no usable OAuth refresh token") from exc


def auth_identity(binary: str, env: dict[str, str]) -> str:
    try:
        proc = subprocess.run([binary, "auth", "status"], env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
        value = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise RotatorError(f"Could not verify Claude login: {exc}") from exc
    if not isinstance(value, dict):
        raise RotatorError("Could not verify Claude login: unexpected `claude auth status` output")
    if proc.returncode != 0 or not value.get("loggedIn") or value.get("authMethod") != "claude.ai":
        raise RotatorError(NOT_SIGNED_IN)
    email = value.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise RotatorError("Claude did not report an email address; cannot verify account")
    org_id = value.get("orgId")
    if org_id is not None and not isinstance(org_id, str):
        raise RotatorError("Claude reported an invalid organization identifier")
    return f"{email.casefold()}|{org_id or ''}"


def state_read() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RotatorError("Shared-login mode is not set up. Click Prepare accounts in the dashboard, or run the setup command, first") from exc
    if not isinstance(value, dict) or not isinstance(value.get("identities"), dict):
        raise RotatorError("Shared-login state is invalid")
    return value


def state_save(value: dict[str, Any]) -> None:
    value["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_json(STATE_FILE, value)


@contextmanager
def shared_lock():
    private_directory(STATE_DIR)
    lock = STATE_DIR / "lock"
    with file_lock(lock):
        yield


@contextmanager
def reader_lock():
    """Held while the monitor's Claude is open. Monitor stops wait for it, so they never cut a token refresh short."""
    private_directory(STATE_DIR)
    with file_lock(STATE_DIR / "reader.lock"):
        yield


def account_by_name(config: Config, name: str) -> Account:
    account = next((a for a in config.accounts if a.name == name), None)
    if account is None:
        raise RotatorError(f"Unknown account: {name}")
    return account


def setup(config: Config, config_path: Path) -> dict[str, Any]:
    if sys.platform not in ("darwin", "win32") and not sys.platform.startswith("linux"):
        raise RotatorError("Shared switching supports macOS, Linux, and Windows")
    binary = claude_binary(config)
    with shared_lock():
        try:
            old = state_read()
        except RotatorError:
            old = {}  # Missing or damaged state: setup is the command that rebuilds it.
        if old.get("enabled"):
            raise RotatorError("Turn off shared-login switching before preparing accounts again")
        identities: dict[str, str] = {}
        credentials: dict[str, str] = {}
        tokens: dict[str, str] = {}
        for account in config.accounts:
            env = base_env()
            env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
            try:
                identity = auth_identity(binary, env)
            except RotatorError as exc:
                if str(exc) == NOT_SIGNED_IN:
                    continue
                raise
            email = identity.split("|", 1)[0]
            if account.email and account.email.casefold() != email:
                raise RotatorError(f"{account.display_name} is signed in as {email}, not {account.email}")
            credentials[account.name] = claude_credential(account.config_dir)
            refresh, _ = credential_parts(credentials[account.name])
            tokens[account.name] = hashlib.sha256(refresh.encode()).hexdigest()
            identities[account.name] = identity
        if len(identities) < 2:
            raise RotatorError("Sign in to at least two Claude profiles before preparing shared mode")
        if len(set(identities.values())) != len(identities):
            raise RotatorError("Each profile must have a different Claude account and organization")
        imported = old.get("imported_tokens") if isinstance(old.get("imported_tokens"), dict) else None
        old_identities = old.get("identities") if isinstance(old.get("identities"), dict) else {}
        old_errors = old.get("login_errors") if isinstance(old.get("login_errors"), dict) else {}
        for account in config.accounts:
            if account.name not in identities:
                continue
            service = vault_service(config_path, account)
            if imported is not None:
                keep = imported.get(account.name) == tokens[account.name]
            else:  # state from before imported_tokens: keep a working vault copy, re-import repaired accounts
                keep = old_identities.get(account.name) == identities[account.name] and account.name not in old_errors
            if keep:
                try:
                    # The profile still holds the token imported last time, which a switch has since spent and
                    # replaced in the vault. Re-importing it would break the account; sign-ins change the token.
                    credential_parts(vault_read(service))
                    continue
                except RotatorError:
                    pass
            keychain_write(service, credentials[account.name])
        current_identity = None
        try:
            current_identity = auth_identity(binary, shared_env())
        except RotatorError:
            pass
        current = next((name for name, identity in identities.items() if identity == current_identity), None)
        state = {"enabled": False, "current": current, "identities": identities, "limited": {},
                 "history": [], "last_error": None,
                 "fallback_on_limit": old.get("fallback_on_limit", True),
                 "vault_version": 4, "imported_tokens": tokens}
        state_save(state)
        return state


def adopt_live_login(state: dict[str, Any], actual: str) -> bool:
    """Follow a shared login that changed outside Switchboard (/login, an interrupted switch) to its prepared account."""
    name = next((name for name, identity in state["identities"].items() if identity == actual), None)
    if name is None:
        return False
    if name != state.get("current"):
        state.setdefault("history", []).append({"from": state.get("current"), "to": name,
                                                 "reason": "signed in outside Switchboard", "at": time.time()})
        state["history"] = state["history"][-50:]
        state["current"] = name
        state.pop("session_usage", None)
    return True


def sync_current(config: Config, config_path: Path, state: dict[str, Any]) -> str | None:
    """Save the live shared login into its account's vault and return it, following the live login when it is
    another prepared account. Returns None when nothing is signed in that could be saved or rolled back to."""
    binary = claude_binary(config)
    try:
        actual = auth_identity(binary, shared_env())
    except RotatorError as exc:
        if str(exc) != NOT_SIGNED_IN:
            raise
        try:
            credential_parts(claude_credential(None))
        except RotatorError:
            state["current"] = None  # signed out with no usable login stored: nothing to keep, a login can only help
            return None
        if state.get("current") is None:
            return None
        raise
    if actual != state["identities"].get(state.get("current")) and not adopt_live_login(state, actual):
        if state.get("current") is None:
            return None  # not a prepared account; there is nothing of ours to save
        raise RotatorError("The shared Claude login changed outside Switchboard; refresh setup before switching")
    account = account_by_name(config, state["current"])
    raw = claude_credential(None)
    credential_parts(raw)
    keychain_write(vault_service(config_path, account), raw)
    state.get("login_errors", {}).pop(account.name, None)  # its saved login is now the live, verified one
    return raw


def claude_login(binary: str, raw: str) -> bool:
    refresh, scopes = credential_parts(raw)
    env = shared_env()
    env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh
    env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes
    # `auth login` replaces the shared credential, so a login cut short can sign every Claude session out.
    # Give it its own session (Ctrl+C, a closing dashboard and Claude's hook cleanup never reach it) and never kill it.
    try:
        proc = popen_detached([binary, "auth", "login", "--claudeai"], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RotatorError(f"Claude login could not run: {exc}") from exc
    refresh_lock = Path.home() / ".claude" / ".oauth_refresh.lock"  # shared_env() has no CLAUDE_CONFIG_DIR
    timeout, deadline = 90, None
    while proc.returncode is None:
        try:
            proc.wait(timeout)
        except KeyboardInterrupt:
            pass  # let it finish; switch() then verifies the login or rolls it back
        except subprocess.TimeoutExpired:
            if deadline is None:
                interrupt_process(proc)  # ask it to stop, then give it a minute to finish cleanly
                deadline, timeout = time.monotonic() + 60, 5
            elif time.monotonic() > deadline and not refresh_lock.exists():
                # Stuck for minutes with no refresh in flight. It holds shared_lock for every switch, so end it.
                interrupt_process(proc, force=True)
    if deadline is not None:
        raise RotatorError("Claude login did not finish within 90 seconds")
    return proc.returncode == 0


def switch(config: Config, config_path: Path, state: dict[str, Any], target: Account,
           reason: str) -> dict[str, Any]:
    if state.get("vault_version") != 4:
        raise RotatorError("Saved accounts need a one-time Keychain update in Switchboard before switching")
    binary = claude_binary(config)
    expected = state["identities"].get(target.name)
    if not expected:
        raise RotatorError(f"{target.display_name} has not been imported into shared-login mode")
    previous_raw = sync_current(config, config_path, state)
    if state.get("current") == target.name:  # the shared login already was this account
        state_save(state)
        return state
    try:
        raw = vault_read(vault_service(config_path, target))
    except RotatorError:
        mark_limited(state, target.name, time.time() + 15 * 60)  # the next tick or report tries another account
        raise
    logged_in = mismatch = False
    try:
        # Claude Code performs the exchange and writes its own shared Keychain login.
        if not claude_login(binary, raw):
            error = f"Claude rejected the saved login for {target.display_name}; sign in to that profile again"
            state.setdefault("login_errors", {})[target.name] = error
            raise RotatorError(error)
        logged_in = True
        actual = auth_identity(binary, shared_env())
        if actual != expected:
            mismatch = True  # its saved login belongs to another account: skip it until it is prepared again
            error = f"Account verification failed: expected {expected}, got {actual}"
            state.setdefault("login_errors", {})[target.name] = error
            raise RotatorError(error)
        fresh = claude_credential(None)
        credential_parts(fresh)
        keychain_write(vault_service(config_path, target), fresh)
    except RotatorError as exc:
        try:
            live = actual if mismatch else auth_identity(binary, shared_env())
        except RotatorError:
            live = None
        if not mismatch and (logged_in or live == expected):
            try:  # the login spent the saved refresh token, even if it failed or timed out; keep its replacement
                fresh = claude_credential(None)
                credential_parts(fresh)
                keychain_write(vault_service(config_path, target), fresh)
            except RotatorError:
                pass  # sync_current() saves the live login before the next switch away
        if live != expected:
            if previous_raw and live != state["identities"].get(state.get("current")):
                try:  # put the previous login back; skipped when it never left, which would only spend its token
                    claude_login(binary, previous_raw)
                except RotatorError:
                    pass
            state["last_error"] = str(exc)
            state_save(state)
            raise
        # The target is live and verified, so the switch happened. Rolling it back would only spend tokens.
    previous = state.get("current")
    state["current"] = target.name
    state["last_error"] = None
    state.get("login_errors", {}).pop(target.name, None)
    state.setdefault("history", []).append({"from": previous, "to": target.name,
                                             "reason": reason, "at": time.time()})
    state["history"] = state["history"][-50:]
    state_save(state)
    return state


def mark_limited(state: dict[str, Any], name: str, reset: float | None = None) -> None:
    """Rest an account until its session window resets (when /usage showed when), else for COOLDOWN_SECONDS."""
    state.setdefault("limited", {})[name] = time.time()
    resets = state.setdefault("resets", {})
    if reset:
        resets[name] = reset
    else:
        resets.pop(name, None)


def resting(state: dict[str, Any], name: str, now: float) -> bool:
    reset = state.get("resets", {}).get(name)
    until = float(reset) if reset else float(state.get("limited", {}).get(name, 0)) + COOLDOWN_SECONDS
    return now < until


def next_account(config: Config, state: dict[str, Any]) -> Account | None:
    names = [account.name for account in config.accounts]
    current = state.get("current")
    start = names.index(current) if current in names else -1
    now = time.time()
    for offset in range(1, len(names) + 1):
        account = config.accounts[(start + offset) % len(names)]
        if (account.name == current or account.name not in state.get("identities", {})
                or account.name in state.get("login_errors", {})
                or resting(state, account.name, now)):
            continue
        percent, _ = usage_percent(account, STATE_DIR.parent)
        if percent is not None and percent >= account.threshold_percent:
            mark_limited(state, account.name)
            continue
        return account
    return None


def rotate(config: Config, config_path: Path, reason: str, expected_current: str | None = None) -> dict[str, Any]:
    with shared_lock():
        state = state_read()
        if not state.get("enabled"):
            return state
        if expected_current and state.get("current") != expected_current:
            return state  # Another terminal's hook already switched the login.
        history = state.get("history", [])
        if reason == "rate_limit" and not state.get("fallback_on_limit", True):
            return state
        last = max(float(history[-1].get("at", 0)) if history else 0.0, float(state.get("limit_attempt_at", 0)))
        if reason == "rate_limit" and time.time() - last < LIMIT_REPORT_GRACE:
            # A late report about the login that was just replaced, or one queued behind a failed attempt.
            return state
        if reason == "rate_limit" and state.get("current"):
            try:
                actual = auth_identity(claude_binary(config), shared_env())
            except RotatorError:
                actual = None
            if actual and actual != state["identities"].get(state["current"]) and adopt_live_login(state, actual):
                state_save(state)  # the report may be about either login; the adoption starts the grace window
                return state
            mark_limited(state, state["current"])
        target = next_account(config, state)
        if target is None:
            state["last_error"] = NO_ACCOUNT_ERROR
            state_save(state)
            return state
        try:
            return switch(config, config_path, state, target, reason)
        except RotatorError as exc:
            state["last_error"] = str(exc)
            if reason == "rate_limit":
                state["limit_attempt_at"] = time.time()  # stamped when the attempt ends; one can outlast the grace
            state_save(state)
            raise


def record_error(exc: Exception, tick: bool = False) -> None:
    """Show an error from a background process on the dashboard, when state can be written at all.
    A monitor tick's error (tick=True) is cleared again by the next healthy tick."""
    try:
        with shared_lock():
            state = state_read()
            state["last_error"] = str(exc)
            if tick:
                state["tick_error"] = str(exc)
            state_save(state)
    except (RotatorError, OSError):
        pass


def hook(config_path: Path) -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("error") != "rate_limit":
        return
    # Claude ends a hook's process tree when its session exits (a `claude -p` run does so right after the error),
    # which could cut `claude auth login` short. Run the switch in its own session and return at once.
    popen_detached([sys.executable, str(Path(__file__).resolve()), "--config", str(config_path), "limit"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def limit(config_path: Path) -> None:
    """Switch away from a login Claude reported as rate limited. Do not trust the event to choose an account."""
    try:
        rotate(load_config(config_path), config_path, "rate_limit")
    except (RotatorError, OSError) as exc:
        record_error(exc)


def hook_command(config_path: Path) -> str:
    args = [sys.executable, str(Path(__file__).resolve()), "--config", str(config_path), "hook"]
    if sys.platform == "win32":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args) + " " + HOOK_MARKER
    return " ".join(shlex.quote(arg) for arg in args) + " " + HOOK_MARKER


def install_hook(config_path: Path, settings_path: Path) -> None:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RotatorError(f"Cannot read Claude settings: {exc}") from exc
    if not isinstance(data, dict):
        raise RotatorError("Claude settings must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RotatorError("Claude settings hooks are invalid")
    entries = hooks.setdefault("StopFailure", [])
    if not isinstance(entries, list):
        raise RotatorError("Claude StopFailure hooks are invalid")
    command = hook_command(config_path)
    for entry in entries:
        if isinstance(entry, dict):
            for item in entry.get("hooks", []):
                if isinstance(item, dict) and HOOK_MARKER in str(item.get("command", "")):
                    item["command"] = command
                    if sys.platform == "win32":
                        item["shell"] = "powershell"
                    break
            else:
                continue
            break
    else:
        item = {"type": "command", "command": command}
        if sys.platform == "win32":
            item["shell"] = "powershell"
        entries.append({"matcher": "rate_limit", "hooks": [item]})
    if settings_path.exists():
        backup = STATE_DIR / "settings-before-shared-login.json"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(settings_path, backup)
            os.chmod(backup, 0o600)
    atomic_json(settings_path, data)


def remove_hook(settings_path: Path) -> None:
    if not settings_path.exists():
        return
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    section = data.get("hooks") if isinstance(data, dict) else None
    entries = section.get("StopFailure") if isinstance(section, dict) else None
    if not isinstance(entries, list):
        return
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        hooks = [item for item in entry.get("hooks", [])
                 if not (isinstance(item, dict) and HOOK_MARKER in str(item.get("command", "")))]
        if hooks:
            copy = dict(entry)
            copy["hooks"] = hooks
            kept.append(copy)
    data["hooks"]["StopFailure"] = kept
    atomic_json(settings_path, data)


def agent_path(config_path: Path) -> tuple[str, Path]:
    suffix = hashlib.sha256(str(config_path).encode()).hexdigest()[:12]
    label = f"com.local.switchboard.shared-login.{suffix}"
    return label, Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def retire_agent(plist: Path, domain: str) -> None:
    """Stop a monitor registered for another config path (an older install or folder): two monitors would fight."""
    try:
        logs = plistlib.loads(plist.read_bytes()).get("StandardOutPath")
    except (OSError, ValueError, plistlib.InvalidFileException):
        logs = None
    state_dir = Path(logs).parent if isinstance(logs, str) else None
    with other_install_locks(state_dir if state_dir != STATE_DIR else None):
        subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, check=False, timeout=10)
    plist.unlink(missing_ok=True)


def install_agent(config_path: Path) -> None:
    if sys.platform != "darwin":
        with reader_lock():
            background_monitor.install(config_path, STATE_DIR, Path(__file__).resolve())
        return
    require_macos()
    label, path = agent_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
                             "--config", str(config_path), "watch"],
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", os.defpath)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(STATE_DIR / "watch.log"),
        "StandardErrorPath": str(STATE_DIR / "watch-error.log"),
    }
    domain = f"gui/{os.getuid()}"
    for other in path.parent.glob("com.local.switchboard.shared-login.*.plist"):
        if other != path:
            retire_agent(other, domain)
    data = plistlib.dumps(value)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as handle:
        os.chmod(temp, 0o600)
        handle.write(data)
    os.replace(temp, path)
    with reader_lock():
        subprocess.run(["launchctl", "bootout", domain, str(path)],
                       capture_output=True, check=False, timeout=10)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                          capture_output=True, check=False, timeout=10)
    if proc.returncode != 0:
        raise RotatorError("Could not start the background usage monitor")


def remove_agent(config_path: Path) -> None:
    with reader_lock():
        if sys.platform != "darwin":
            background_monitor.remove(config_path, STATE_DIR)
            return
        label, path = agent_path(config_path)
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
                           capture_output=True, check=False, timeout=10)
            path.unlink()


def watch(config: Config | None, config_path: Path, interval: int = 30) -> None:
    """Background monitor loop. It must outlive bad ticks: launchd/systemd restart a crashed monitor in a loop."""
    while True:
        time.sleep(interval)
        try:
            check_usage(config_path)
        except Exception as exc:  # any bad tick: record it and keep monitoring
            record_error(exc, tick=True)


def check_usage(config_path: Path) -> None:
    """One monitor tick: read the current session percentage and switch at the account's threshold."""
    config = load_config(config_path)
    with shared_lock():
        state = state_read()
        current = state.get("current")
        if not state.get("enabled") or not current:
            return
        account = account_by_name(config, current)
    reset = None
    try:
        with reader_lock():
            if account.usage_command:
                percent, error = usage_percent(account, STATE_DIR.parent)
            else:
                percent, error, reset = read_session_usage(claude_binary(config), shared_env(), STATE_DIR.parent)
    except (RotatorError, OSError) as exc:
        percent, error = None, str(exc)
    with shared_lock():
        state = state_read()
        if not state.get("enabled") or state.get("current") != current:
            return
        state["session_usage"] = {"account": current, "percent": percent,
                                  "checked_at": time.time(), "error": error}
        try:
            actual = auth_identity(claude_binary(config), shared_env())
            if actual != state["identities"].get(current):
                if adopt_live_login(state, actual):
                    state_save(state)
                    return  # measure the adopted account on the next tick
                raise RotatorError("The shared login changed outside Switchboard. Update prepared accounts before resuming session checks.")
        except RotatorError as exc:
            if percent is not None:
                state["session_usage"].update({"percent": None, "error": str(exc)})
            state_save(state)
            return
        if percent is None or percent < account.threshold_percent:
            if percent is not None:  # fresh evidence this account has room again
                state.get("limited", {}).pop(current, None)
                state.get("resets", {}).pop(current, None)
                if state.get("last_error") in (NO_ACCOUNT_ERROR, state.pop("tick_error", None)):
                    state["last_error"] = None
            state_save(state)
            return
        if time.time() - float(state.get("limit_attempt_at", 0)) < LIMIT_REPORT_GRACE:
            state_save(state)
            return  # an attempt just failed; give it the same pause as rate-limit reports get
        mark_limited(state, current, reset)
        target = next_account(config, state)
        if target is None:
            state["last_error"] = NO_ACCOUNT_ERROR
            state_save(state)
            return
        try:
            switch(config, config_path, state, target, f"{percent:g}% usage")
        except RotatorError as exc:
            state["last_error"] = str(exc)
            state["limit_attempt_at"] = time.time()  # reports queued behind this attempt must not repeat it
            state_save(state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DATA_DIR / "rotator.json")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("setup", help="Import signed-in profiles into the OS protected credential store")
    sub.add_parser("enable", help="Enable limit hooks and shared-login rotation")
    sub.add_parser("disable", help="Disable rotation and remove its Claude hook")
    sub.add_parser("status", help="Show shared-login mode state")
    switch_parser = sub.add_parser("switch", help="Switch the shared login now")
    switch_parser.add_argument("account")
    sub.add_parser("hook", help="Handle Claude StopFailure input")
    sub.add_parser("watch", help="Check current-session usage and switch at the saved threshold")
    fallback_parser = sub.add_parser("fallback", help="Allow or disable limit-error fallback switching")
    fallback_parser.add_argument("choice", choices=("on", "off"))
    sub.add_parser("ensure-monitor", help="Start the session usage monitor if switching is already enabled")
    sub.add_parser("migrate", help="Move older account credentials to the prompt-free Keychain store")
    sub.add_parser("limit", help="Switch away from a rate-limited login (started by the hook)")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    use_config(config_path)
    if config_path.parent.is_dir():
        # Hooks start in the Claude session's folder, where Windows looks up claude.exe first. Not the package
        # folder either: a long-running monitor there blocks npm upgrades on Windows.
        os.chdir(config_path.parent)
    if args.action in ("enable", "ensure-monitor", "migrate") and "_npx" in Path(__file__).resolve().parts:
        # The hook and the monitor run this file later; npx deletes its cached copies at any time.
        raise RotatorError("Automatic switching needs an installed copy of cswitch. Run: npm install -g cswitch")
    # Only these read the config up front; disable and status must keep working while rotator.json is broken.
    config = load_config(config_path) if args.action in ("setup", "enable", "switch", "migrate") else None
    settings = Path.home() / ".claude" / "settings.json"
    if args.action == "setup":
        state = setup(config, config_path)
        print(f"Imported {len(state['identities'])} accounts. Current shared account: {state['current'] or 'none'}")
    elif args.action == "enable":
        with shared_lock():
            state = state_read()
            sync_current(config, config_path, state)  # follows a /login to a prepared account
            if not state.get("current"):
                raise RotatorError("The shared login is not a prepared account yet. Run switch <account> (or Switch account in the dashboard) first")
            install_hook(config_path, settings)
            try:
                install_agent(config_path)
            except (RotatorError, OSError, subprocess.TimeoutExpired):
                remove_hook(settings)
                raise
            state["enabled"] = True
            state["enabled_at"] = time.time()
            if str(state.get("last_error") or "").startswith(MONITOR_ERROR):
                state["last_error"] = None
            state_save(state)
        print("Shared-login rotation enabled for local Claude terminals using the default configuration")
    elif args.action == "disable":
        with shared_lock():
            state = state_read()
            state["enabled"] = False
            state_save(state)
            remove_hook(settings)
            remove_agent(config_path)
        print("Shared-login rotation disabled")
    elif args.action == "status":
        state = state_read()
        print(json.dumps({"enabled": bool(state.get("enabled")),
                          "current": state.get("current"),
                          "fallback_on_limit": state.get("fallback_on_limit", True),
                          "session_usage": state.get("session_usage"),
                          "prepared_accounts": list(state.get("identities", {})),
                          "last_error": state.get("last_error"),
                          "history": state.get("history", [])[-5:]}, indent=2))
    elif args.action == "switch":
        with shared_lock():
            state = state_read()
            state = switch(config, config_path, state, account_by_name(config, args.account), "manual")
        print(f"Shared Claude login: {state['current']}")
    elif args.action == "fallback":
        with shared_lock():
            state = state_read()
            state["fallback_on_limit"] = args.choice == "on"
            state_save(state)
        print("Limit fallback " + ("on" if state["fallback_on_limit"] else "off"))
    elif args.action == "ensure-monitor":
        with shared_lock():
            state = state_read()
            if state.get("enabled") and state.get("vault_version") == 4:
                install_hook(config_path, settings)  # the interpreter or script path may have moved (npm upgrade)
                install_agent(config_path)
                if str(state.get("last_error") or "").startswith(MONITOR_ERROR):
                    state["last_error"] = None
                    state_save(state)
    elif args.action == "migrate":
        with shared_lock():
            state = migrate_vault(config, config_path, state_read())
            if state.get("enabled"):
                install_agent(config_path)
        print("Saved account logins updated. Manual and automatic switching use the same trusted Keychain access.")
    elif args.action == "hook":
        hook(config_path)
    elif args.action == "limit":
        limit(config_path)
    elif args.action == "watch":
        windows_monitor_job()
        watch(config, config_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RotatorError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
