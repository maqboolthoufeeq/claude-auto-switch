"""Read the current subscription session percentage from Claude's own /usage screen."""

from __future__ import annotations

import codecs
import os
import re
import select
import signal
import subprocess
import sys
import time
import pyte

from rotator import BASE_DIR
from platform_runtime import kill_windows_tree


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
    clean_env = dict(env)
    clean_env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "120"})
    windows = sys.platform == "win32"
    argv = [binary, "--safe-mode", "--ax-screen-reader"]
    if windows:
        try:
            from winpty import PTY
            process = PTY(120, 120)
            environment = "\0".join(f"{key}={value}" for key, value in sorted(clean_env.items())) + "\0"
            if not process.spawn(binary, cwd=str(BASE_DIR), env=environment,
                                 cmdline=" " + subprocess.list2cmdline(argv[1:])):
                return None, "Could not start Claude's Windows usage terminal"
        except ImportError:
            return None, "Windows session usage needs pywinpty. Run uv sync and reopen Switchboard."
        except Exception:
            return None, "Could not start the Windows usage terminal. Check claude --version in Terminal."
    else:
        import fcntl
        import pty
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 120, 120, 0, 0))
        try:
            process = subprocess.Popen(argv, cwd=BASE_DIR, env=clean_env,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except OSError as exc:
            os.close(master)
            os.close(slave)
            return None, f"Could not start Claude usage check: {exc}"
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
                    return None, "Claude's Windows usage terminal closed before a percentage could be read"
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
                    return None, "Claude needs a one-time interactive setup in the Switchboard project folder before session usage can be checked"
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
                    return percent, None
            alive = process.isalive() if windows else process.poll() is None
            if not alive:
                break
        return None, "Claude did not provide a current-session usage percentage; switching at a confirmed limit remains available"
    except (OSError, EOFError):
        return None, "Claude's usage terminal closed before a session percentage could be read"
    finally:
        if windows:
            if process.isalive() and process.pid:
                kill_windows_tree(process.pid)
        else:
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
