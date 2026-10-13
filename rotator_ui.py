#!/usr/bin/env python3
"""Private local dashboard for the Claude Code account rotator."""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from rotator import (
    BASE_DIR,
    RotatorError,
    atomic_json,
    auth_status,
    claude_binary,
    load_config,
    profile_env,
    state_paths,
)
from shared_mode import STATE_FILE, old_vault_service, previous_vault_service, shared_env, shared_lock, state_read, state_save, vault_delete, vault_service


ASSETS = {
    "/": (BASE_DIR / "ui" / "index.html", "text/html; charset=utf-8"),
    "/app.css": (BASE_DIR / "ui" / "app.css", "text/css; charset=utf-8"),
    "/app.js": (BASE_DIR / "ui" / "app.js", "text/javascript; charset=utf-8"),
}
DASHBOARD_PORT = 56729


def save_dashboard_url(config_path: Path, url: str) -> Path:
    """Let the launcher reopen this server without exposing its token in the page."""
    directory = config_path.parent / ".shared-rotator"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "dashboard-url"
    temporary = directory / f".dashboard-url-{secrets.token_hex(8)}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(url + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class Dashboard:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.process_kind: str | None = None
        self.process_project: Path | None = None
        self.last_exit_code: int | None = None
        self.messages: deque[str] = deque(maxlen=100)
        self.status_cache: tuple[float, list[dict[str, object]]] | None = None
        self.account_operation = False
        self.activity_lock = threading.Lock()
        self.activity_file = config_path.parent / ".shared-rotator" / "account-activity.json"

    def _config(self):
        return load_config(self.config_path)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _activity(self) -> dict[str, object]:
        try:
            value = json.loads(self.activity_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _save_activity(self, value: dict[str, object]) -> None:
        self.activity_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(self.activity_file, value)

    def _account_event(self, name: str, kind: str, message: str) -> None:
        with self.activity_lock:
            data = self._activity()
            entry = data.setdefault(name, {})
            if not isinstance(entry, dict):
                entry = {}
                data[name] = entry
            when = self._timestamp()
            events = entry.setdefault("events", [])
            if not isinstance(events, list):
                events = []
            events.append({"at": when, "kind": kind, "message": message})
            entry["events"] = events[-30:]
            if kind == "login":
                entry.setdefault("first_seen_connected_at", when)
                entry["last_login_at"] = when
                entry["connected_since"] = when
            elif kind == "logout":
                entry["last_logout_at"] = when
                entry["connected_since"] = None
            self._save_activity(data)

    def _observe_connection(self, name: str, signed_in: bool, reason: str) -> None:
        with self.activity_lock:
            data = self._activity()
            entry = data.setdefault(name, {})
            if not isinstance(entry, dict):
                entry = {}
                data[name] = entry
            if signed_in and entry.get("connected_since"):
                return
            if not signed_in and (not entry.get("connected_since") or reason.startswith("cannot check")):
                return
            when = self._timestamp()
            events = entry.get("events", [])
            if not isinstance(events, list):
                events = []
            if signed_in:
                entry.setdefault("first_seen_connected_at", when)
                entry["connected_since"] = when
                message = "Switchboard observed this profile signed in"
            else:
                entry["connected_since"] = None
                message = "Switchboard observed this profile signed out"
            events.append({"at": when, "kind": "observed", "message": message})
            entry["events"] = events[-30:]
            self._save_activity(data)

    def accounts(self) -> list[dict[str, object]]:
        now = time.monotonic()
        with self.lock:
            cache = self.status_cache
        if cache and now - cache[0] < 5:
            return cache[1]
        config = self._config()
        binary = claude_binary(config)

        def describe(account):
            signed_in, reason = auth_status(binary, account)
            return {
                "name": account.name,
                "display_name": account.display_name,
                "email": account.email,
                "signed_in": signed_in,
                "status": reason,
                "threshold_percent": account.threshold_percent,
                "early_usage_check": True,
            }

        with ThreadPoolExecutor(max_workers=min(8, len(config.accounts))) as pool:
            results = list(pool.map(describe, config.accounts))
        for item in results:
            self._observe_connection(str(item["name"]), bool(item["signed_in"]), str(item["status"]))
        with self.lock:
            self.status_cache = (time.monotonic(), results)
        return results

    def _record(self, message: str) -> None:
        with self.lock:
            self.messages.append(message.rstrip()[:1500])

    def _start(self, argv: list[str], kind: str, project: Path | None = None,
               env: dict[str, str] | None = None) -> None:
        with self.lock:
            if self.account_operation or (self.process is not None and self.process.poll() is None):
                raise RotatorError("A task or sign-in is already running")
            self.messages.clear()
            self.last_exit_code = None
            try:
                process = subprocess.Popen(
                    argv, cwd=BASE_DIR, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, start_new_session=True,
                )
            except OSError as exc:
                raise RotatorError(f"Could not start {kind}: {exc}") from exc
            self.process = process
            self.process_kind = kind
            self.process_project = project
        self._record(f"{kind.capitalize()} started")
        threading.Thread(target=self._read_output, args=(process, kind), daemon=True).start()

    def _read_output(self, process: subprocess.Popen[str], kind: str) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if not kind.startswith("login:"):
                self._record(line)
        code = process.wait()
        process.stdout.close()
        if kind.startswith("login:") and code != 0:
            self._record("Sign-in failed. Try the account's login command in Terminal if the browser asks for a code.")
        if kind.startswith("login:") and code == 0:
            self._account_event(kind.split(":", 1)[1], "login", "Signed in through Switchboard")
        self._record(f"{kind.capitalize()} finished with exit code {code}")
        with self.lock:
            if self.process is process:
                self.last_exit_code = code
                self.status_cache = None

    def start_task(self, project_text: str, task: str | None) -> None:
        if not project_text.strip():
            raise RotatorError("Choose a project folder")
        project = Path(project_text).expanduser().resolve()
        if not project.is_dir():
            raise RotatorError("Choose an existing project folder")
        if task is not None and not task.strip():
            raise RotatorError("Describe the task before starting")
        state_file, _, _ = state_paths(project)
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = None
        except (OSError, json.JSONDecodeError) as exc:
            raise RotatorError(f"Cannot read this project's saved task: {exc}") from exc
        if task is not None and isinstance(saved, dict) and saved.get("status") != "complete":
            raise RotatorError("This project has a saved task. Use Continue saved task first")
        if task is None and saved is None:
            raise RotatorError("No saved task exists in this project")
        command = [sys.executable, str(BASE_DIR / "rotator.py"),
                   "--config", str(self.config_path)]
        if task is None:
            command.extend(["resume", "--project", str(project)])
        else:
            command.extend(["run", "--project", str(project), task.strip()])
        self._start(command, "task", project)

    def login(self, name: str) -> None:
        config = self._config()
        account = next((item for item in config.accounts if item.name == name), None)
        if account is None:
            raise RotatorError("Unknown account")
        account.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = [claude_binary(config), "auth", "login"]
        if account.email:
            command.extend(["--email", account.email])
        self._start(command, f"login:{name}", env=profile_env(account))

    def shared_status(self) -> dict[str, object]:
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        config = self._config()
        identities = state.get("identities", {})
        if not isinstance(identities, dict):
            identities = {}
        return {
            "prepared": bool(identities),
            "imported_count": len(identities),
            "enabled": bool(state.get("enabled")),
            "current": state.get("current"),
            "last_error": state.get("last_error"),
            "login_errors": state.get("login_errors", {}),
            "history": state.get("history", [])[-10:],
            "early_usage_check": True,
            "fallback_on_limit": state.get("fallback_on_limit", True),
            "session_usage": state.get("session_usage"),
            "needs_keychain_update": bool(identities) and state.get("vault_version") != 4,
            "old_keychain_cleanup_pending": state.get("old_vault_cleanup_pending", []),
            "accounts": [{"name": a.name, "display_name": a.display_name,
                          "prepared": a.name in identities}
                         for a in config.accounts],
        }

    def account_info(self, name: str) -> dict[str, object]:
        config = self._config()
        account = next((item for item in config.accounts if item.name == name), None)
        if account is None:
            raise RotatorError("This account is no longer in Switchboard")
        status = next(item for item in self.accounts() if item["name"] == name)
        with self.activity_lock:
            activity = self._activity().get(name, {})
        if not isinstance(activity, dict):
            activity = {}
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        identities = state.get("identities", {})
        if not isinstance(identities, dict):
            identities = {}
        identity = identities.get(name, "")
        email, _, org_id = identity.partition("|") if isinstance(identity, str) else ("", "", "")
        events = list(activity.get("events", [])) if isinstance(activity.get("events"), list) else []
        for event in state.get("history", []) if isinstance(state.get("history"), list) else []:
            if not isinstance(event, dict) or name not in (event.get("from"), event.get("to")):
                continue
            try:
                when = datetime.fromtimestamp(float(event["at"]), timezone.utc).isoformat(timespec="seconds")
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            direction = "Switched into this account" if event.get("to") == name else "Switched away from this account"
            reason = " after a usage limit" if event.get("reason") == "rate_limit" else " at the session threshold" if "% usage" in str(event.get("reason")) else " manually"
            events.append({"at": when, "kind": "switch", "message": direction + reason})
        events = sorted((event for event in events if isinstance(event, dict)),
                        key=lambda event: str(event.get("at", "")), reverse=True)[:12]
        return {
            "name": account.name,
            "display_name": account.display_name,
            "email": email or account.email or None,
            "organization_id": org_id or None,
            "profile_dir": str(account.config_dir),
            "signed_in": status["signed_in"],
            "login_error": state.get("login_errors", {}).get(name),
            "status": status["status"],
            "prepared": name in identities,
            "current": state.get("current") == name,
            "switching_enabled": bool(state.get("enabled")),
            "first_seen_connected_at": activity.get("first_seen_connected_at"),
            "connected_since": activity.get("connected_since"),
            "last_login_at": activity.get("last_login_at"),
            "last_logout_at": activity.get("last_logout_at"),
            "checked_at": self._timestamp(),
            "events": events,
        }

    def _auth_logout(self, binary: str, env: dict[str, str]) -> None:
        try:
            result = subprocess.run([binary, "auth", "logout"], env=env,
                                    capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RotatorError(f"Claude logout could not finish: {exc}") from exc
        if result.returncode != 0:
            raise RotatorError("Claude could not sign out this account. Try again or sign out in Claude Code.")

    def remove_account(self, name: str, delete: bool = False) -> None:
        config = self._config()
        account = next((item for item in config.accounts if item.name == name), None)
        if account is None:
            raise RotatorError("This account is no longer in Switchboard")
        if delete and len(config.accounts) == 1:
            raise RotatorError("Keep at least one account in Switchboard")
        with self.lock:
            if self.account_operation or (self.process is not None and self.process.poll() is None):
                raise RotatorError("Wait for the current operation to finish")
            self.account_operation = True
        try:
            binary = claude_binary(config)
            with shared_lock():
                try:
                    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                identities = state.get("identities", {})
                if not isinstance(identities, dict):
                    identities = {}
                current = state.get("current") == name
                enabled = bool(state.get("enabled"))
                if current and enabled:
                    raise RotatorError("Switch the shared login to another account before signing this one out")
                if enabled and name in identities and len(identities) <= 2:
                    raise RotatorError("Turn off automatic switching before removing one of its last two accounts")
                if current:
                    self._auth_logout(binary, shared_env())
                self._auth_logout(binary, profile_env(account))
                if name in identities:
                    vault_delete(vault_service(self.config_path, account))
                    vault_delete(old_vault_service(self.config_path, account))
                    vault_delete(previous_vault_service(self.config_path, account))
                    identities.pop(name, None)
                    state["identities"] = identities
                    pending = state.get("old_vault_cleanup_pending", [])
                    if isinstance(pending, list):
                        state["old_vault_cleanup_pending"] = [item for item in pending if item != name]
                    limited = state.get("limited", {})
                    if isinstance(limited, dict):
                        limited.pop(name, None)
                    state.get("login_errors", {}).pop(name, None)
                    if current:
                        state["current"] = None
                    if delete and isinstance(state.get("history"), list):
                        state["history"] = [event for event in state["history"]
                                            if isinstance(event, dict) and name not in (event.get("from"), event.get("to"))]
                    state["updated_at"] = self._timestamp()
                    atomic_json(STATE_FILE, state)
                elif current:
                    state["current"] = None
                    state["updated_at"] = self._timestamp()
                    atomic_json(STATE_FILE, state)
            if delete:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                data["accounts"] = [item for item in data["accounts"] if item.get("name") != name]
                atomic_json(self.config_path, data)
                with self.activity_lock:
                    activity = self._activity()
                    activity.pop(name, None)
                    self._save_activity(activity)
            else:
                self._account_event(name, "logout", "Signed out of this profile and removed its prepared login")
        finally:
            with self.lock:
                self.account_operation = False
                self.status_cache = None

    def shared_action(self, action: str, account: str = "") -> None:
        if action not in {"setup", "enable", "disable", "switch", "fallback", "migrate"}:
            raise RotatorError("Unknown shared-login action")
        command = [sys.executable, str(BASE_DIR / "shared_mode.py"),
                   "--config", str(self.config_path), action]
        if action == "switch":
            if not any(a.name == account for a in self._config().accounts):
                raise RotatorError("Unknown account")
            command.append(account)
        elif action == "fallback":
            if account not in {"on", "off"}:
                raise RotatorError("Choose whether limit fallback should be on or off")
            command.append(account)
        self._start(command, f"shared-{action}")

    def set_limit_fallback(self, enabled: bool) -> None:
        with shared_lock():
            state = state_read()
            state["fallback_on_limit"] = enabled
            state_save(state)

    def stop(self) -> None:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                raise RotatorError("Nothing is running")
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        self._record("Stop requested")

    def shutdown(self) -> None:
        with self.lock:
            process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)

    def task_status(self, project_text: str | None) -> dict[str, object]:
        with self.lock:
            process = self.process
            running = process is not None and process.poll() is None
            kind = self.process_kind
            active_project = self.process_project
            messages = list(self.messages)
            exit_code = self.last_exit_code
        project = Path(project_text).expanduser().resolve() if project_text else active_project
        visible_running = running and (project is None or project == active_project)
        saved: dict[str, object] | None = None
        if project and project.is_dir():
            state_file, _, _ = state_paths(project)
            try:
                value = json.loads(state_file.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    saved = {
                        "status": value.get("status"),
                        "task": value.get("task"),
                        "last_summary": value.get("last_summary"),
                        "runs": value.get("runs", []),
                        "next_account_index": value.get("next_account_index"),
                        "updated_at": value.get("updated_at"),
                    }
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "running": visible_running,
            "busy": running,
            "kind": kind,
            "exit_code": exit_code,
            "active_project": str(active_project) if active_project else None,
            "messages": messages,
            "saved": saved,
        }

    @staticmethod
    def _display_name(value: str, fallback: str) -> str:
        label = value.strip() or fallback
        if len(label) > 60 or not label.isprintable():
            raise RotatorError("Display name must be 1–60 printable characters")
        return label

    def add_account(self, name: str, email: str, display_name: str = "") -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise RotatorError("Use letters, numbers, hyphens, or underscores for the account name")
        label = self._display_name(display_name, name)
        with self.lock:
            if self.account_operation or (self.process is not None and self.process.poll() is None):
                raise RotatorError("Wait for the current run before changing accounts")
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if any(item.get("name") == name for item in data["accounts"]):
                raise RotatorError("That account name is already in use")
            if any((item.get("display_name") or item.get("name", "")).casefold() == label.casefold()
                   for item in data["accounts"]):
                raise RotatorError("That display name is already in use")
            data["accounts"].append({
                "name": name, "display_name": label, "email": email.strip(),
                "config_dir": f"profile/{name}",
                "threshold_percent": 95, "usage_command": None,
            })
            atomic_json(self.config_path, data)
            self.status_cache = None
        self._account_event(name, "added", "Added to Switchboard")

    def set_display_name(self, name: str, display_name: str) -> None:
        label = self._display_name(display_name, name)
        with self.lock:
            if self.account_operation or (self.process is not None and self.process.poll() is None):
                raise RotatorError("Wait for the current run before changing settings")
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            account = next((item for item in data["accounts"] if item.get("name") == name), None)
            if account is None:
                raise RotatorError("Unknown account")
            if any(item is not account and
                   (item.get("display_name") or item.get("name", "")).casefold() == label.casefold()
                   for item in data["accounts"]):
                raise RotatorError("That display name is already in use")
            account["display_name"] = label
            atomic_json(self.config_path, data)
            self.status_cache = None
        self._account_event(name, "renamed", f"Renamed to {label}")

    def set_threshold(self, name: str, percent: float) -> None:
        if not 0 < percent <= 100:
            raise RotatorError("Threshold must be between 1 and 100")
        with self.lock:
            if self.account_operation or (self.process is not None and self.process.poll() is None):
                raise RotatorError("Wait for the current run before changing settings")
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            account = next((item for item in data["accounts"] if item.get("name") == name), None)
            if account is None:
                raise RotatorError("Unknown account")
            account["threshold_percent"] = percent
            atomic_json(self.config_path, data)
            self.status_cache = None
        self._account_event(name, "settings", f"Usage threshold set to {percent:g}%")


def make_handler(dashboard: Dashboard, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _headers(self, content_type: str, length: int, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def _send_json(self, value: object, status: int = 200) -> None:
            body = json.dumps(value).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body), status)
            self.wfile.write(body)

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "")
            expected = f"127.0.0.1:{self.server.server_port}"
            if host != expected or self.headers.get("X-Rotator-Token") != token:
                self._send_json({"error": "Access denied"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{expected}":
                self._send_json({"error": "Access denied"}, 403)
                return False
            return True

        def do_GET(self) -> None:
            route = urlsplit(self.path)
            if route.path in ASSETS:
                host = self.headers.get("Host", "")
                if host != f"127.0.0.1:{self.server.server_port}":
                    self._send_json({"error": "Access denied"}, 403)
                    return
                file, content_type = ASSETS[route.path]
                body = file.read_bytes()
                self._headers(content_type, len(body))
                self.wfile.write(body)
                return
            if not self._authorized():
                return
            if route.path == "/api/accounts":
                self._send_json({"accounts": dashboard.accounts()})
            elif route.path == "/api/account":
                name = parse_qs(route.query).get("name", [""])[0]
                try:
                    self._send_json(dashboard.account_info(name))
                except RotatorError as exc:
                    self._send_json({"error": str(exc)}, 400)
            elif route.path == "/api/task":
                project = parse_qs(route.query).get("project", [None])[0]
                self._send_json(dashboard.task_status(project))
            elif route.path == "/api/shared":
                self._send_json(dashboard.shared_status())
            else:
                self._send_json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536 or length < 2:
                self._send_json({"error": "Invalid request size"}, 400)
                return
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                route = urlsplit(self.path).path
                if route == "/api/run":
                    dashboard.start_task(str(data.get("project", "")), str(data.get("task", "")))
                elif route == "/api/resume":
                    dashboard.start_task(str(data.get("project", "")), None)
                elif route == "/api/stop":
                    dashboard.stop()
                elif route == "/api/login":
                    dashboard.login(str(data.get("name", "")))
                elif route == "/api/logout":
                    dashboard.remove_account(str(data.get("name", "")))
                elif route == "/api/delete-account":
                    dashboard.remove_account(str(data.get("name", "")), delete=True)
                elif route == "/api/accounts":
                    dashboard.add_account(str(data.get("name", "")), str(data.get("email", "")),
                                          str(data.get("display_name", "")))
                elif route == "/api/display-name":
                    dashboard.set_display_name(str(data.get("name", "")), str(data.get("display_name", "")))
                elif route == "/api/threshold":
                    dashboard.set_threshold(str(data.get("name", "")), float(data.get("percent", 0)))
                elif route == "/api/fallback":
                    if not isinstance(data.get("enabled"), bool):
                        raise RotatorError("Choose whether limit fallback should be on or off")
                    dashboard.set_limit_fallback(data["enabled"])
                elif route == "/api/shared":
                    dashboard.shared_action(str(data.get("action", "")), str(data.get("account", "")))
                elif route == "/api/pick-project":
                    if sys.platform != "darwin":
                        raise RotatorError("Enter the project folder path on this computer")
                    result = subprocess.run(
                        ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose the code project")'],
                        capture_output=True, text=True, timeout=120, check=False,
                    )
                    if result.returncode != 0:
                        raise RotatorError("No folder was selected")
                    self._send_json({"project": result.stdout.strip()})
                    return
                else:
                    self._send_json({"error": "Not found"}, 404)
                    return
                self._send_json({"ok": True})
            except (RotatorError, ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "rotator.json")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    load_config(config_path)
    dashboard = Dashboard(config_path)
    try:
        existing_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(existing_state, dict) and existing_state.get("enabled"):
            proc = subprocess.run(
                [sys.executable, str(BASE_DIR / "shared_mode.py"), "--config", str(config_path), "ensure-monitor"],
                capture_output=True, text=True, timeout=25, check=False,
            )
            if proc.returncode != 0:
                dashboard._record("Session usage monitor could not start. Turn switching off and on to retry.")
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    token = secrets.token_urlsafe(32)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), make_handler(dashboard, token))
    except OSError as exc:
        raise RotatorError(f"Could not open Switchboard at http://127.0.0.1:{DASHBOARD_PORT}/. Close any older Switchboard launcher and try again: {exc}") from exc
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    try:
        url_path = save_dashboard_url(config_path, url)
    except OSError:
        server.server_close()
        raise RotatorError("Could not save the private dashboard link. Check the project folder's permissions and try again.")
    print(f"Rotator dashboard: {url}", flush=True)
    print("Keep this window open while a task runs. Press Ctrl+C to close the dashboard.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.signal(signal.SIGHUP, stop_on_signal)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.shutdown()
        server.server_close()
        try:
            if url_path.read_text(encoding="utf-8").strip() == url:
                url_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RotatorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
