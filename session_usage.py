"""Read the current subscription session percentage from Claude's own /usage screen."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
import pyte

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


def read_session_usage(binary: str, env: dict[str, str], timeout: float = 35) -> tuple[float | None, str | None]:
    """Open a short-lived, safe-mode Claude TUI and ask its built-in /usage command."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 120, 120, 0, 0))
    clean_env = dict(env)
    clean_env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "120"})
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
    screen = pyte.Screen(120, 120)
    stream = pyte.Stream(screen)
    requested = False
    last_activity = time.monotonic()
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
                stream.feed(chunk.decode("utf-8", errors="replace"))
                last_activity = time.monotonic()
                clean = "\n".join(screen.display)
                if any(prompt in clean for prompt in ("Permission Required:", "Choose the text style", "Enter y/n:")):
                    return None, "Claude needs a one-time interactive setup in the Switchboard project folder before session usage can be checked"
                if not requested and "Claude Code v" in clean and "$" in clean:
                    os.write(master, b"/usage\r")
                    requested = True
            if requested:
                rendered = "\n".join(screen.display)
                percent = parse_session_usage(rendered)
                if (percent is not None and "Refreshing" not in rendered
                        and time.monotonic() - last_activity >= 0.75):
                    return percent, None
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
