#!/usr/bin/env python3
"""Rotate the Claude login shared by independent local terminal sessions."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

from rotator import (
    BASE_DIR, Account, Config, ENV_REMOVE, RotatorError, atomic_json,
    claude_binary, load_config, usage_percent,
)
from session_usage import read_session_usage


STATE_DIR = BASE_DIR / ".shared-rotator"
STATE_FILE = STATE_DIR / "state.json"
HOOK_MARKER = "# switchboard-shared-login"
COOLDOWN_SECONDS = 5 * 60 * 60


def require_macos() -> None:
    if sys.platform != "darwin":
        raise RotatorError("Shared-login mode currently requires macOS Keychain")


def base_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in ENV_REMOVE}


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
    return f"Switchboard-v3-{scope}-{account.name}"


def old_vault_service(config_path: Path, account: Account) -> str:
    scope = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]
    return f"Switchboard-v2-{scope}-{account.name}"


def keychain_read(service: str) -> str:
    require_macos()
    proc = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RotatorError(f"No readable Keychain credential for {service}")
    return proc.stdout.rstrip("\n")


def claude_credential(config_dir: Path | None) -> str:
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


def vault_read(service: str) -> str:
    proc = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                           "-s", service, "-w"], capture_output=True, timeout=30, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RotatorError(f"No readable Switchboard Keychain credential for {service}")
    try:
        return base64.b64decode(proc.stdout.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise RotatorError(f"Saved Keychain credential is damaged for {service}") from exc


def old_vault_read(service: str) -> str:
    binary = keychain_helper_binary()
    proc = subprocess.run([str(binary), "read", service], capture_output=True,
                          timeout=30, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RotatorError(f"Could not read the older saved credential for {service}")
    return proc.stdout.decode("utf-8")


def vault_delete(service: str) -> None:
    proc = subprocess.run(["/usr/bin/security", "delete-generic-password", "-a", getpass.getuser(),
                           "-s", service], capture_output=True,
                          timeout=30, check=False)
    if proc.returncode not in (0, 44):
        raise RotatorError(f"Could not remove saved Keychain credential for {service}")


def keychain_write(service: str, secret: str) -> None:
    encoded = base64.b64encode(secret.encode("utf-8"))
    proc = subprocess.run(["/usr/bin/security", "add-generic-password", "-a", getpass.getuser(),
                           "-s", service, "-U", "-w"], input=encoded + b"\n" + encoded + b"\n",
                          capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RotatorError(f"Could not save Keychain credential for {service}")


def migrate_vault(config: Config, config_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Move older per-account items to items owned by Apple's signed security tool."""
    prepared = [account for account in config.accounts if account.name in state.get("identities", {})]
    if state.get("vault_version") != 3:
        for account in prepared:
            service = vault_service(config_path, account)
            try:
                credential_parts(vault_read(service))
                continue
            except RotatorError:
                pass
            try:
                raw = old_vault_read(old_vault_service(config_path, account))
            except RotatorError:
                raw = claude_credential(account.config_dir)
            credential_parts(raw)
            keychain_write(service, raw)
        state["vault_version"] = 3
    cleanup_pending = []
    for account in prepared:
        try:
            vault_delete(old_vault_service(config_path, account))
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
                              text=True, timeout=20, check=False)
        value = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise RotatorError(f"Could not verify Claude login: {exc}") from exc
    if proc.returncode != 0 or not value.get("loggedIn") or value.get("authMethod") != "claude.ai":
        raise RotatorError("Claude is not signed in with a claude.ai subscription")
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
        raise RotatorError("Shared-login mode is not set up. Run shared_mode.py setup first") from exc
    if not isinstance(value, dict) or not isinstance(value.get("identities"), dict):
        raise RotatorError("Shared-login state is invalid")
    return value


def state_save(value: dict[str, Any]) -> None:
    value["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_json(STATE_FILE, value)


@contextmanager
def shared_lock():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = STATE_DIR / "lock"
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def account_by_name(config: Config, name: str) -> Account:
    account = next((a for a in config.accounts if a.name == name), None)
    if account is None:
        raise RotatorError(f"Unknown account: {name}")
    return account


def setup(config: Config, config_path: Path) -> dict[str, Any]:
    require_macos()
    binary = claude_binary(config)
    with shared_lock():
        if STATE_FILE.exists():
            old = state_read()
            if old.get("enabled"):
                raise RotatorError("Turn off shared-login switching before preparing accounts again")
        identities: dict[str, str] = {}
        credentials: dict[str, str] = {}
        for account in config.accounts:
            env = base_env()
            env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
            try:
                identity = auth_identity(binary, env)
            except RotatorError as exc:
                if str(exc) == "Claude is not signed in with a claude.ai subscription":
                    continue
                raise
            email = identity.split("|", 1)[0]
            if account.email and account.email.casefold() != email:
                raise RotatorError(f"{account.display_name} is signed in as {email}, not {account.email}")
            credentials[account.name] = claude_credential(account.config_dir)
            credential_parts(credentials[account.name])
            identities[account.name] = identity
        if len(identities) < 2:
            raise RotatorError("Sign in to at least two Claude profiles before preparing shared mode")
        if len(set(identities.values())) != len(identities):
            raise RotatorError("Each profile must have a different Claude account and organization")
        for account in config.accounts:
            if account.name not in identities:
                continue
            keychain_write(vault_service(config_path, account), credentials[account.name])
        current_identity = None
        try:
            current_identity = auth_identity(binary, shared_env())
        except RotatorError:
            pass
        current = next((name for name, identity in identities.items() if identity == current_identity), None)
        state = {"enabled": False, "current": current, "identities": identities, "limited": {},
                 "history": [], "last_error": None,
                 "fallback_on_limit": old.get("fallback_on_limit", True) if STATE_FILE.exists() else True,
                 "vault_version": 3}
        state_save(state)
        return state


def sync_current(config: Config, config_path: Path, state: dict[str, Any]) -> str | None:
    current = state.get("current")
    if current is None:
        return None
    binary = claude_binary(config)
    actual = auth_identity(binary, shared_env())
    if actual != state["identities"].get(current):
        raise RotatorError("The shared Claude login changed outside Switchboard; refresh setup before switching")
    account = account_by_name(config, current)
    raw = claude_credential(None)
    credential_parts(raw)
    keychain_write(vault_service(config_path, account), raw)
    return raw


def claude_login(binary: str, raw: str) -> bool:
    refresh, scopes = credential_parts(raw)
    env = shared_env()
    env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh
    env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes
    try:
        proc = subprocess.run([binary, "auth", "login", "--claudeai"], env=env,
                              capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError(f"Claude login could not run: {exc}") from exc
    return proc.returncode == 0


def switch(config: Config, config_path: Path, state: dict[str, Any], target: Account,
           reason: str) -> dict[str, Any]:
    if state.get("vault_version") != 3:
        raise RotatorError("Saved accounts need a one-time Keychain update in Switchboard before switching")
    binary = claude_binary(config)
    expected = state["identities"].get(target.name)
    if not expected:
        raise RotatorError(f"{target.display_name} has not been imported into shared-login mode")
    if state.get("current") == target.name:
        return state
    previous_raw = sync_current(config, config_path, state)
    raw = vault_read(vault_service(config_path, target))
    try:
        # Claude Code performs the exchange and writes its own shared Keychain login.
        if not claude_login(binary, raw):
            raise RotatorError(f"Claude rejected the saved login for {target.display_name}; sign in to that profile again")
        actual = auth_identity(binary, shared_env())
        if actual != expected:
            raise RotatorError(f"Account verification failed: expected {expected}, got {actual}")
    except RotatorError:
        if previous_raw:
            try:
                claude_login(binary, previous_raw)
            except RotatorError:
                pass
        raise
    fresh = claude_credential(None)
    credential_parts(fresh)
    keychain_write(vault_service(config_path, target), fresh)
    previous = state.get("current")
    state["current"] = target.name
    state["last_error"] = None
    state.setdefault("history", []).append({"from": previous, "to": target.name,
                                             "reason": reason, "at": time.time()})
    state["history"] = state["history"][-50:]
    state_save(state)
    return state


def next_account(config: Config, state: dict[str, Any]) -> Account | None:
    names = [account.name for account in config.accounts]
    current = state.get("current")
    start = names.index(current) if current in names else -1
    now = time.time()
    limited = state.setdefault("limited", {})
    for offset in range(1, len(names) + 1):
        account = config.accounts[(start + offset) % len(names)]
        if (account.name == current or account.name not in state.get("identities", {})
                or now - float(limited.get(account.name, 0)) < COOLDOWN_SECONDS):
            continue
        percent, _ = usage_percent(account, BASE_DIR)
        if percent is not None and percent >= account.threshold_percent:
            limited[account.name] = now
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
        if reason == "rate_limit" and history and time.time() - float(history[-1].get("at", 0)) < 15:
            return state  # Concurrent terminals may report the same exhausted login.
        if reason == "rate_limit" and state.get("current"):
            state.setdefault("limited", {})[state["current"]] = time.time()
        target = next_account(config, state)
        if target is None:
            state["last_error"] = "No account is available to switch to"
            state_save(state)
            return state
        try:
            return switch(config, config_path, state, target, reason)
        except RotatorError as exc:
            state["last_error"] = str(exc)
            state_save(state)
            raise


def hook(config: Config, config_path: Path) -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("error") != "rate_limit":
            return
        # Do not trust the event to choose an account. The serialized state does that.
        rotate(config, config_path, "rate_limit")
    except (RotatorError, ValueError, json.JSONDecodeError) as exc:
        with shared_lock():
            try:
                state = state_read()
                state["last_error"] = str(exc)
                state_save(state)
            except RotatorError:
                pass


def hook_command(config_path: Path) -> str:
    args = [sys.executable, str(Path(__file__).resolve()), "--config", str(config_path), "hook"]
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
                    break
            else:
                continue
            break
    else:
        entries.append({"matcher": "rate_limit", "hooks": [{"type": "command", "command": command}]})
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
    entries = data.get("hooks", {}).get("StopFailure", [])
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


def install_agent(config_path: Path) -> None:
    require_macos()
    label, path = agent_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
                             "--config", str(config_path), "watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(STATE_DIR / "watch.log"),
        "StandardErrorPath": str(STATE_DIR / "watch-error.log"),
    }
    data = plistlib.dumps(value)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as handle:
        os.chmod(temp, 0o600)
        handle.write(data)
    os.replace(temp, path)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)],
                   capture_output=True, check=False, timeout=10)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                          capture_output=True, check=False, timeout=10)
    if proc.returncode != 0:
        raise RotatorError("Could not start the background usage monitor")


def remove_agent(config_path: Path) -> None:
    label, path = agent_path(config_path)
    if path.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
                       capture_output=True, check=False, timeout=10)
        path.unlink()


def watch(config: Config, config_path: Path, interval: int = 30) -> None:
    while True:
        time.sleep(interval)
        config = load_config(config_path)
        with shared_lock():
            state = state_read()
            if not state.get("enabled"):
                continue
            current = state.get("current")
            if not current:
                continue
            account = account_by_name(config, current)
        if account.usage_command:
            percent, error = usage_percent(account, BASE_DIR)
        else:
            percent, error = read_session_usage(claude_binary(config), shared_env())
        with shared_lock():
            state = state_read()
            if not state.get("enabled") or state.get("current") != current:
                continue
            state["session_usage"] = {"account": current, "percent": percent,
                                      "checked_at": time.time(), "error": error}
            if percent is not None:
                try:
                    if auth_identity(claude_binary(config), shared_env()) != state["identities"].get(current):
                        raise RotatorError("The shared login changed outside Switchboard. Update prepared accounts before resuming session checks.")
                except RotatorError as exc:
                    state["session_usage"].update({"percent": None, "error": str(exc)})
                    state_save(state)
                    continue
            state_save(state)
            if percent is not None and percent >= account.threshold_percent:
                state.setdefault("limited", {})[current] = time.time()
                target = next_account(config, state)
                if target is not None:
                    try:
                        switch(config, config_path, state, target, f"{percent:g}% usage")
                    except RotatorError as exc:
                        state["last_error"] = str(exc)
                        state_save(state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "rotator.json")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("setup", help="Import signed-in profiles into private macOS Keychain slots")
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
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    settings = Path.home() / ".claude" / "settings.json"
    if args.action == "setup":
        state = setup(config, config_path)
        print(f"Imported {len(state['identities'])} accounts. Current shared account: {state['current'] or 'none'}")
    elif args.action == "enable":
        with shared_lock():
            state = state_read()
            if not state.get("current"):
                raise RotatorError("Sign in to one imported account in plain Claude first, then run setup again")
            sync_current(config, config_path, state)
            install_hook(config_path, settings)
            try:
                install_agent(config_path)
            except (RotatorError, OSError, subprocess.TimeoutExpired):
                remove_hook(settings)
                raise
            state["enabled"] = True
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
            if state.get("enabled") and state.get("vault_version") == 3:
                install_agent(config_path)
    elif args.action == "migrate":
        with shared_lock():
            state = migrate_vault(config, config_path, state_read())
            if state.get("enabled"):
                install_agent(config_path)
        print("Saved account logins updated. Manual and automatic switching use the same trusted Keychain access.")
    elif args.action == "hook":
        hook(config, config_path)
    elif args.action == "watch":
        watch(config, config_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RotatorError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
