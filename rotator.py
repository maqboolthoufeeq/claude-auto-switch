#!/usr/bin/env python3
"""Continue one Claude Code task across separately signed-in Claude subscription profiles."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_runtime import RotatorError, file_lock, interrupt_process, process_options, private_directory, check_macos_version


BASE_DIR = Path(__file__).resolve().parent
# User data (config, profiles, state). The npm launcher sets CSWITCH_HOME; a git checkout keeps data beside the code.
DATA_DIR = Path(os.environ.get("CSWITCH_HOME") or BASE_DIR).expanduser().resolve()
SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["complete", "continue", "blocked"]},
        "summary": {"type": "string"},
        "next_steps": {"type": "string"},
    },
    "required": ["status", "summary", "next_steps"],
    "additionalProperties": False,
}
LIMIT_PATTERN = re.compile(
    r"(?:usage limit|out of usage|usage exhausted|"
    r"you(?:'|’)ve (?:reached|hit) (?:your|the) |"
    r"5-hour limit reached|session limit reached|weekly limit reached|"
    r"limit reached.{0,100}resets?)",
    re.IGNORECASE,
)
AUTH_PATTERN = re.compile(
    r"login expired|please (?:log in|login|run /login)|not logged in", re.IGNORECASE
)
ENV_REMOVE = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
}


@dataclass(frozen=True)
class Account:
    name: str
    display_name: str
    email: str
    config_dir: Path
    threshold_percent: float
    usage_command: list[str] | None


@dataclass(frozen=True)
class Config:
    claude_command: str
    permission_mode: str
    max_runs: int
    accounts: list[Account]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(50):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open for reading for a moment.
                if sys.platform != "win32" or attempt == 49:
                    raise
                time.sleep(0.02)
    finally:
        temp.unlink(missing_ok=True)


def load_config(path: Path, data: Any = None) -> Config:
    """Read and validate a config; pass `data` to validate edits before they are saved to `path`."""
    if data is None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RotatorError(f"Cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RotatorError("Config must be a JSON object")
    command = data.get("claude_command", "claude")
    mode = data.get("permission_mode", "auto")
    max_runs = data.get("max_runs", 20)
    if not isinstance(command, str) or not command:
        raise RotatorError("claude_command must be a nonempty string")
    if mode not in {"auto", "acceptEdits", "dontAsk"}:
        raise RotatorError("permission_mode must be auto, acceptEdits, or dontAsk")
    if type(max_runs) is not int or max_runs < 1:
        raise RotatorError("max_runs must be a positive integer")
    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise RotatorError("accounts must be a nonempty list")
    accounts: list[Account] = []
    names: set[str] = set()
    display_names: set[str] = set()
    directories: set[Path] = set()
    for item in raw_accounts:
        if not isinstance(item, dict):
            raise RotatorError("Each account must be an object")
        name = item.get("name")
        display_name = item.get("display_name", name)
        email = item.get("email", "")
        raw_dir = item.get("config_dir")
        threshold = item.get("threshold_percent", 95)
        usage_command = item.get("usage_command")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise RotatorError("Account names may contain letters, digits, hyphens, and underscores")
        if name in names:
            raise RotatorError(f"Duplicate account name: {name}")
        if (not isinstance(display_name, str) or not display_name.strip()
                or len(display_name.strip()) > 60 or not display_name.isprintable()):
            raise RotatorError(f"display_name for {name} must be 1–60 printable characters")
        display_name = display_name.strip()
        if display_name.casefold() in display_names:
            raise RotatorError(f"Duplicate display name: {display_name}")
        if not isinstance(email, str):
            raise RotatorError(f"email for {name} must be a string")
        if not isinstance(raw_dir, str) or not raw_dir:
            raise RotatorError(f"config_dir is required for {name}")
        profile_path = Path(raw_dir).expanduser()
        config_dir = (profile_path if profile_path.is_absolute() else path.parent / profile_path).resolve()
        if config_dir == path.parent:
            raise RotatorError(f"config_dir for {name} must not be the config folder itself")
        if config_dir in directories:
            raise RotatorError(f"Each account needs its own config_dir: {config_dir}")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 100:
            raise RotatorError(f"threshold_percent for {name} must be between 0 and 100")
        if usage_command is not None and (
            not isinstance(usage_command, list)
            or not usage_command
            or not all(isinstance(part, str) and part for part in usage_command)
        ):
            raise RotatorError(f"usage_command for {name} must be an array of command arguments or null")
        accounts.append(Account(name, display_name, email, config_dir, float(threshold), usage_command))
        names.add(name)
        display_names.add(display_name.casefold())
        directories.add(config_dir)
    return Config(command, mode, max_runs, accounts)


def profile_env(account: Account) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in ENV_REMOVE}
    env["PYTHONIOENCODING"] = "utf-8"
    env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
    return env


def claude_binary(config: Config) -> str:
    check_macos_version()
    command = "claude.exe" if sys.platform == "win32" and config.claude_command == "claude" else config.claude_command
    found = shutil.which(command)
    if not found and config.claude_command == "claude":
        candidates = ([Path.home() / ".local/bin/claude.exe"] if sys.platform == "win32"
                      else [Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")])
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found = str(candidate)
                break
        if not found and sys.platform == "win32":
            found = shutil.which("claude")
    if not found:
        raise RotatorError(f"Claude Code command not found: {config.claude_command}")
    if sys.platform == "win32" and Path(found).suffix.lower() in (".cmd", ".bat"):
        raise RotatorError("The configured Claude command is a Windows batch file. Install Claude Code's native claude.exe to preserve task arguments safely.")
    return found


def auth_status(binary: str, account: Account) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [binary, "auth", "status"], env=profile_env(account),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return False, f"cannot check login ({exc})"
    method = data.get("authMethod", "none")
    if proc.returncode != 0 or not data.get("loggedIn"):
        return False, "not signed in"
    if method != "claude.ai":
        return False, f"using {method}, not a Claude subscription login"
    return True, "signed in"


def usage_percent(account: Account, project: Path) -> tuple[float | None, str | None]:
    if account.usage_command is None:
        return None, None
    env = profile_env(account)
    env["CLAUDE_ROTATOR_ACCOUNT"] = account.name
    env["CLAUDE_ROTATOR_PROJECT"] = str(project)
    try:
        proc = subprocess.run(
            account.usage_command, cwd=project, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
        )
        if proc.returncode != 0:
            return None, f"usage check failed for {account.display_name} (exit {proc.returncode})"
        output = proc.stdout.strip()
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = output
        percent = parsed.get("used_percent") if isinstance(parsed, dict) else parsed
        percent = float(percent)
        if not 0 <= percent <= 100:
            raise ValueError("percentage outside 0–100")
        return percent, None
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
        return None, f"usage check failed for {account.display_name}: {exc}"


def select_account(config: Config, binary: str, project: Path, start: int,
                   excluded: set[str]) -> tuple[int, Account] | None:
    for offset in range(len(config.accounts)):
        index = (start + offset) % len(config.accounts)
        account = config.accounts[index]
        if account.name in excluded:
            continue
        logged_in, reason = auth_status(binary, account)
        if not logged_in:
            print(f"Skipping {account.display_name}: {reason}", flush=True)
            continue
        percent, error = usage_percent(account, project)
        if error:
            print(f"Warning: {error}; continuing without an early threshold", flush=True)
        if percent is not None and percent >= account.threshold_percent:
            print(f"Skipping {account.display_name}: {percent:g}% used (threshold {account.threshold_percent:g}%)", flush=True)
            continue
        return index, account
    return None


def task_prompt(state: dict[str, Any], handoff: Path) -> str:
    return (
        "Continue this software task autonomously in the current project.\n\n"
        f"Original task:\n{state['task']}\n\n"
        f"Read {handoff} first, then inspect the actual project files and changes. "
        "The handoff may be incomplete if a previous account hit its limit mid-step. "
        "Continue from the code as it exists; do not repeat completed work. "
        "Update the handoff file after each substantial step with completed work, "
        "files changed, checks run, and remaining steps. Keep it concise and accurate. "
        "When the task is fully finished and verified, return status complete. "
        "If more work remains and you can continue, return status continue. "
        "Return blocked only when progress truly requires user input or approval. "
        "Put a useful summary and specific next_steps in the required structured output."
    )


def run_claude(binary: str, config: Config, account: Account, project: Path,
               prompt: str, log_dir: Path, session_id: str | None) -> tuple[int, dict[str, Any] | None, str]:
    private_directory(log_dir)
    stdout_path = log_dir / "stdout.json"
    stderr_path = log_dir / "stderr.txt"
    prompt_path = log_dir / "prompt.txt"
    fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    # The prompt goes in on stdin: argv is visible to every local user in the process list.
    argv = [binary, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
            "--permission-mode", config.permission_mode, "--permission-prompts", "none"]
    if session_id:
        argv.extend(["--resume", session_id])
    env = profile_env(account)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr, \
            prompt_path.open("rb") as stdin:
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        proc = subprocess.Popen(argv, cwd=project, env=env, stdin=stdin,
                                stdout=stdout, stderr=stderr, **process_options())
        try:
            while True:
                try:
                    code = proc.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    print(f"{account.display_name} is still working…", flush=True)
        except KeyboardInterrupt:
            interrupt_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                interrupt_process(proc, force=True)
                proc.wait()
            raise
    raw = stdout_path.read_text(encoding="utf-8", errors="replace")
    errors = stderr_path.read_text(encoding="utf-8", errors="replace")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = None
    return code, result, (raw + "\n" + errors)[-12000:]


def is_usage_limit(code: int, result: dict[str, Any] | None, output: str) -> bool:
    failed = code != 0 or (isinstance(result, dict) and result.get("is_error"))
    if not failed:
        return False
    message = output
    if isinstance(result, dict):
        if result.get("api_error") == "usage_limit_reached":
            return True
        message += "\n" + str(result.get("result", ""))
    return bool(LIMIT_PATTERN.search(message))


def state_paths(project: Path) -> tuple[Path, Path, Path]:
    folder = project / ".claude-rotator"
    return folder / "state.json", folder / "HANDOFF.md", folder / "logs"


def ensure_project_ignore(project: Path, config: Config) -> None:
    """Keep task logs and any local account profiles out of a target Git repository."""
    proc = subprocess.run(["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return
    ignore = project / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    entries = {".claude-rotator/"}
    for account in config.accounts:
        if project in account.config_dir.parents:
            relative = account.config_dir.relative_to(project)
            entries.add(f"/{relative.as_posix()}/")
            tracked = subprocess.run(
                ["git", "-C", str(project), "ls-files", "--", str(relative)],
                capture_output=True, text=True, check=False,
            )
            if tracked.returncode != 0 or tracked.stdout.strip():
                raise RotatorError(f"Profile {account.name} contains files tracked by Git; remove them from Git first")
    missing = sorted(entries - set(current.splitlines()))
    if not missing:
        return
    with ignore.open("a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n".join(missing) + "\n")


@contextmanager
def task_lock(project: Path):
    folder = project / ".claude-rotator"
    private_directory(folder)
    lock_path = folder / "lock"
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(lock_path, blocking=False))
        except BlockingIOError as exc:
            raise RotatorError("Another rotator run is already active in this project") from exc
        yield


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_json(path, state)


def execute_task(config: Config, project: Path, task: str | None) -> int:
    binary = claude_binary(config)
    state_path, handoff, logs = state_paths(project)
    if task is not None:
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RotatorError(f"No readable task state at {state_path}: {exc}") from exc
            if not isinstance(previous, dict):
                raise RotatorError(f"Invalid task state at {state_path}")
            if previous.get("status") != "complete":
                raise RotatorError(f"A task is already saved in {state_path}; use resume")
        state = {"task_id": uuid.uuid4().hex[:12], "task": task, "status": "running", "next_account_index": 0,
                 "session_id": None, "session_account": None, "runs": [], "last_summary": ""}
        handoff.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(handoff, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"# Task handoff\n\n## Original task\n\n{task}\n\n## Progress\n\nNo work recorded yet.\n")
        save_state(state_path, state)
    else:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RotatorError(f"No readable task state at {state_path}: {exc}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("task"), str) or not isinstance(state.get("runs"), list):
            raise RotatorError(f"Invalid task state at {state_path}")
        if type(state.get("next_account_index")) is not int:
            raise RotatorError(f"Invalid account position in {state_path}")
        if state.get("status") == "complete":
            print("The saved task is already complete.")
            return 0
        state["status"] = "running"
        save_state(state_path, state)

    excluded: set[str] = set()
    task_logs = logs / state.get("task_id", "legacy")
    while len(state["runs"]) < config.max_runs:
        selected = select_account(config, binary, project, state["next_account_index"], excluded)
        if selected is None:
            state["status"] = "paused"
            save_state(state_path, state)
            print("No signed-in account has available usage. Progress is saved; run resume after a reset or login.")
            return 2
        index, account = selected
        same_session = state.get("session_account") == account.name
        session_id = state.get("session_id") if same_session else None
        run_number = len(state["runs"]) + 1
        log_dir = task_logs / f"run-{run_number:03d}-{account.name}"
        print(f"Run {run_number}: {account.display_name}", flush=True)
        try:
            code, result, output = run_claude(
                binary, config, account, project, task_prompt(state, handoff), log_dir, session_id
            )
        except KeyboardInterrupt:
            state["status"] = "paused"
            save_state(state_path, state)
            print("Stopped. Progress is saved; run resume to continue.")
            return 130
        record = {"account": account.name, "exit_code": code, "log_dir": str(log_dir),
                  "session_id": result.get("session_id") if isinstance(result, dict) else None}
        state["runs"].append(record)
        if is_usage_limit(code, result, output):
            print(f"{account.display_name} reached its usage limit. Switching profiles.", flush=True)
            excluded.add(account.name)
            state["next_account_index"] = (index + 1) % len(config.accounts)
            state["session_id"] = None
            state["session_account"] = None
            record["outcome"] = "usage_limit"
            save_state(state_path, state)
            continue
        if code != 0 or not isinstance(result, dict) or result.get("is_error"):
            if AUTH_PATTERN.search(output):
                print(f"{account.display_name} needs to sign in again. Switching profiles.", flush=True)
                excluded.add(account.name)
                state["next_account_index"] = (index + 1) % len(config.accounts)
                state["session_id"] = None
                state["session_account"] = None
                record["outcome"] = "auth_error"
                save_state(state_path, state)
                continue
            state["status"] = "paused"
            record["outcome"] = "error"
            save_state(state_path, state)
            print(f"Claude stopped with an error on {account.display_name}. See {log_dir}.")
            return 1
        structured = result.get("structured_output")
        if not isinstance(structured, dict) or structured.get("status") not in {"complete", "continue", "blocked"}:
            state["status"] = "paused"
            record["outcome"] = "invalid_result"
            save_state(state_path, state)
            print(f"Claude returned no valid task status. See {log_dir}.")
            return 1
        state["last_summary"] = structured.get("summary", "")
        state["session_id"] = result.get("session_id")
        state["session_account"] = account.name
        record["outcome"] = structured["status"]
        record["summary"] = structured.get("summary", "")
        print(f"{account.display_name}: {structured['status']} — {structured.get('summary', '')}", flush=True)
        if structured["status"] == "complete":
            state["status"] = "complete"
            save_state(state_path, state)
            print(f"Task complete. Handoff: {handoff}")
            return 0
        if structured["status"] == "blocked":
            state["status"] = "paused"
            save_state(state_path, state)
            print(f"Needs input: {structured.get('next_steps', '')}")
            return 2
        state["next_account_index"] = index
        save_state(state_path, state)
    state["status"] = "paused"
    save_state(state_path, state)
    print(f"Reached max_runs ({config.max_runs}). Progress is saved; increase it and run resume.")
    return 2


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        import signal
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DATA_DIR / "rotator.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create an editable five-account config")
    login_parser = sub.add_parser("login", help="Open Claude's browser login for one account")
    login_parser.add_argument("account")
    sub.add_parser("status", help="Check each account's Claude login")
    run_parser = sub.add_parser("run", help="Start a new task")
    run_parser.add_argument("--project", type=Path, required=True)
    run_parser.add_argument("task", help="The task to complete, or - to read it from standard input")
    resume_parser = sub.add_parser("resume", help="Continue the saved task")
    resume_parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run" and args.task == "-":
        args.task = sys.stdin.read()
    config_path = args.config.expanduser().resolve()
    if args.command == "init":
        if config_path.exists():
            raise RotatorError(f"Config already exists: {config_path}")
        shutil.copyfile(BASE_DIR / "rotator.example.json", config_path)
        print(f"Created {config_path}. Edit profile names and paths, then run login for each account.")
        return 0
    config = load_config(config_path)
    binary = claude_binary(config)
    if args.command == "login":
        account = next((item for item in config.accounts if item.name == args.account), None)
        if account is None:
            raise RotatorError(f"Unknown account: {args.account}")
        private_directory(account.config_dir)
        command = [binary, "auth", "login"]
        if account.email:
            command.extend(["--email", account.email])
        return subprocess.call(command, env=profile_env(account))
    if args.command == "status":
        for account in config.accounts:
            _, status = auth_status(binary, account)
            title = account.display_name if account.display_name == account.name else f"{account.display_name} ({account.name})"
            print(f"{title}: {status}")
        return 0
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise RotatorError(f"Project directory does not exist: {project}")
    with task_lock(project):
        ensure_project_ignore(project, config)
        return execute_task(config, project, args.task if args.command == "run" else None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RotatorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
