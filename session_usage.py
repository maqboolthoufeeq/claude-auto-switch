"""Read the current subscription session percentage from Claude's own /usage screen."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time

from rotator import BASE_DIR


OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SESSION = re.compile(r"Current session\b(?:(?!Current week).){0,300}?(\d+(?:\.\d+)?)%\s*used", re.I | re.S)


def parse_session_usage(output: str) -> float | None:
    """Select only the session quota, never the context or weekly percentage."""
    clean = CSI.sub("", OSC.sub("", output)).replace("\r", "\n")
    match = SESSION.search(clean)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 100 else None


def cached_session_usage(path: Path, now: float) -> float | None:
    """Accept only a recent session quota belonging to Claude's current login."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cache = data["cachedUsageUtilization"]
        identity = data["oauthAccount"]["accountUuid"]
        age = now - float(cache["fetchedAtMs"]) / 1000
        value = cache["utilization"]["five_hour"]["utilization"]
        if (not identity or cache["accountUuid"] != identity or not 0 <= age <= 60
                or isinstance(value, bool) or not isinstance(value, (int, float))):
            return None
        return float(value) if 0 <= value <= 100 else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def read_session_usage(binary: str, env: dict[str, str], timeout: float = 35) -> tuple[float | None, str | None]:
    """Open a short-lived, safe-mode Claude TUI and ask its built-in /usage command."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    clean_env = dict(env)
    clean_env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
    try:
        process = subprocess.Popen(
            [binary, "--safe-mode", "--ax-screen-reader"], cwd=BASE_DIR, env=clean_env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        return None, f"Could not start Claude usage check: {exc}"
    os.close(slave)
    output = ""
    requested = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], min(0.5, max(0, deadline - time.monotonic())))
            if ready:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output = (output + chunk.decode("utf-8", errors="replace"))[-64000:]
                clean = CSI.sub("", OSC.sub("", output)).replace("\r", "\n")
                if any(prompt in clean for prompt in ("Permission Required:", "Choose the text style", "Enter y/n:")):
                    return None, "Claude needs a one-time interactive setup in the Switchboard project folder before session usage can be checked"
                if not requested and "Claude Code v" in clean and "$" in clean:
                    os.write(master, b"/usage\r")
                    requested = True
            if requested and parse_session_usage(output) is not None:
                # The screen may initially show an older cached bar while refreshing.
                fresh = cached_session_usage(Path.home() / ".claude.json", time.time())
                if fresh is not None:
                    return fresh, None
            if process.poll() is not None:
                break
        return None, "Claude did not provide a current-session usage percentage; switching at a confirmed limit remains available"
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        os.close(master)
