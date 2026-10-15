"""Read the current subscription session percentage from Claude's own /usage screen."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
import pyte

from rotator import DATA_DIR
from platform_runtime import kill_windows_tree


OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SESSION = re.compile(r"Current session\b(?:(?!Current week).){0,300}?(\d+(?:\.\d+)?)%\s*used", re.I | re.S)
# "Resets 7:10am", "Resets 3pm (Europe/Berlin)", "Resets 15:00": an hour followed by minutes or am/pm.
RESET = re.compile(r"Resets\b[^\n]*?\b(\d{1,2})(?::(\d{2})(?:\s*([ap])\.?m\.?)?|\s*([ap])\.?m\.?)(?!\w)", re.I)


def parse_session_usage(output: str) -> float | None:
    """Select only the session quota, never the context or weekly percentage."""
    clean = CSI.sub("", OSC.sub("", output)).replace("\r", "\n")
    match = SESSION.search(clean)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 100 else None


def parse_session_reset(output: str, now: float | None = None) -> float | None:
    """When the current session window resets, as a timestamp, if the screen shows it within the next 5 hours."""
    clean = CSI.sub("", OSC.sub("", output)).replace("\r", "\n")
    start = clean.find("Current session")
    if start < 0:
        return None
    end = clean.find("Current week", start)
    match = RESET.search(clean[start:end if end > 0 else None])
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    half = (match.group(3) or match.group(4) or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    now = time.time() if now is None else now
    day = time.localtime(now)
    reset = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0, 0, 0, -1))
    if reset <= now:
        reset += 24 * 60 * 60
    return reset if reset - now <= 5 * 60 * 60 + 300 else None


def read_session_usage(binary: str, env: dict[str, str], cwd: Path = DATA_DIR,
                       timeout: float = 35) -> tuple[float | None, str | None, float | None]:
    """Open a short-lived, safe-mode Claude TUI and ask its built-in /usage command.

    Returns the session percentage, an error message, and the session reset time when the screen shows one."""
    clean_env = dict(env)
    clean_env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "120"})
    windows = sys.platform == "win32"
    argv = [binary, "--safe-mode", "--ax-screen-reader"]
    if windows:
        try:
            from winpty import PTY
            process = PTY(120, 120)
            environment = "\0".join(f"{key}={value}" for key, value in sorted(clean_env.items())) + "\0"
            if not process.spawn(binary, cwd=str(cwd), env=environment,
                                 cmdline=" " + subprocess.list2cmdline(argv[1:])):
                return None, "Could not start Claude's Windows usage terminal", None
        except ImportError:
            return None, "Windows session usage needs pywinpty. Reopen Switchboard so it can install it (in a git checkout, run uv sync).", None
        except Exception:
            return None, "Could not start the Windows usage terminal. Check claude --version in Terminal.", None
    else:
        import fcntl
        import pty
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 120, 120, 0, 0))
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=clean_env,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except OSError as exc:
            os.close(master)
            os.close(slave)
            return None, f"Could not start Claude usage check: {exc}", None
        os.close(slave)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    screen = pyte.Screen(120, 120)
    stream = pyte.Stream(screen)
    requested = False
    last_activity = time.monotonic()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if windows:
                try:
                    chunk = process.read(blocking=False)
                except Exception:
                    return None, "Claude's Windows usage terminal closed before a percentage could be read", None
                if not chunk:
                    time.sleep(0.05)
            else:
                ready, _, _ = select.select([master], [], [], min(0.5, max(0, deadline - time.monotonic())))
                chunk = ""
                if ready:
                    try:
                        data = os.read(master, 8192)
                    except OSError:
                        break
                    if not data:
                        break
                    chunk = decoder.decode(data)
            if chunk:
                stream.feed(chunk)
                last_activity = time.monotonic()
                clean = "\n".join(screen.display)
                if any(prompt in clean for prompt in ("Permission Required:", "Choose the text style", "Enter y/n:")):
                    return None, f"Claude needs a one-time setup before session usage can be checked: open a terminal in {cwd}, run claude, and accept its prompts", None
                if not requested and "Claude Code v" in clean and "$" in clean:
                    if windows:
                        process.write("/usage\r")
                    else:
                        os.write(master, b"/usage\r")
                    requested = True
            if requested:
                rendered = "\n".join(screen.display)
                percent = parse_session_usage(rendered)
                if (percent is not None and "Refreshing" not in rendered
                        and time.monotonic() - last_activity >= 0.75):
                    return percent, None, parse_session_reset(rendered)
            alive = process.isalive() if windows else process.poll() is None
            if not alive:
                break
        return None, "Claude did not provide a current-session usage percentage; switching at a confirmed limit remains available", None
    except (OSError, EOFError):
        return None, "Claude's usage terminal closed before a session percentage could be read", None
    finally:
        stop_claude(process, None if windows else master, clean_env)
        if not windows:
            os.close(master)


def stop_claude(process, master, env: dict[str, str], grace: float = 8.0) -> None:
    """Ask Claude to quit and wait for it. Killing it mid token-refresh leaves the shared login with a
    refresh token the server has already retired, and every other open session then fails with
    "OAuth token revoked" / "Not logged in" until the next switch."""
    windows = master is None

    def alive() -> bool:
        return bool(process.isalive()) if windows else process.poll() is None

    try:
        for keys in ("\x1b", "/exit\r"):  # close the usage panel, then quit the way a user would
            if alive():
                process.write(keys) if windows else os.write(master, keys.encode())
                time.sleep(0.2)
        end = time.monotonic() + grace
        while alive() and time.monotonic() < end:
            # Keep draining the terminal so Claude never blocks on a full pty while shutting down.
            if windows:
                process.read(blocking=False)
            elif select.select([master], [], [], 0)[0]:
                os.read(master, 8192)
            time.sleep(0.1)
    except Exception:  # pty closed, pywinpty read after exit: the process is gone or going
        pass
    lock = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".oauth_refresh.lock"
    end = time.monotonic() + 20
    while alive() and lock.exists() and time.monotonic() < end:
        time.sleep(0.5)  # a refresh is in flight; let it finish before forcing anything
    if windows:
        if alive() and process.pid:
            kill_windows_tree(process.pid)
        return
    if alive():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
