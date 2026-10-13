"""Install a monitor for the current user on Linux or native Windows."""

from __future__ import annotations

import hashlib
import csv
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from platform_runtime import RotatorError, powershell_env


def identity(config_path: Path) -> str:
    return "switchboard-" + hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]


def linux_unit_path(config_path: Path) -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "systemd" / "user" / (identity(config_path) + ".service")


def systemd_quote(value: str, command: bool = False) -> str:
    value = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def run_checked(argv: list[str], error: str) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError(error) from exc
    if result.returncode != 0:
        raise RotatorError(error)


def install(config_path: Path, state_dir: Path, script: Path) -> None:
    argv = [sys.executable, str(script), "--config", str(config_path), "watch"]
    if sys.platform.startswith("linux"):
        path = linux_unit_path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        environment = ["Environment=" + systemd_quote(f"{key}={os.environ[key]}")
                       for key in ("PATH", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY") if key in os.environ]
        content = ("[Unit]\nDescription=Switchboard session usage monitor\n"
                   "[Service]\nType=simple\nExecStart=" + " ".join(systemd_quote(arg, command=True) for arg in argv) + "\n"
                   + "\n".join(environment) + "\nRestart=on-failure\nRestartSec=10\n"
                   "[Install]\nWantedBy=default.target\n")
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        error = "Cannot start the background monitor. Linux needs systemd user services in your logged-in desktop session."
        run_checked(["systemctl", "--user", "daemon-reload"], error)
        run_checked(["systemctl", "--user", "enable", path.name], error)
        run_checked(["systemctl", "--user", "restart", path.name], error)
    elif sys.platform == "win32":
        state_dir.mkdir(parents=True, exist_ok=True)
        task = windows_task(argv)
        path = state_dir / "monitor-task.xml"
        path.write_bytes(task)
        error = "Cannot register the background monitor. Allow your user account to create a Task Scheduler task, then retry."
        # IgnoreNew would otherwise leave the previous interpreter running after an app update.
        subprocess.run(["schtasks", "/End", "/TN", identity(config_path)], capture_output=True, timeout=20, check=False)
        run_checked(["schtasks", "/Create", "/TN", identity(config_path), "/XML", str(path), "/F"], error)
        run_checked(["schtasks", "/Run", "/TN", identity(config_path)], error)
        wait_windows_task(identity(config_path))
    else:
        raise RotatorError("No background monitor is available on this operating system")


def wait_windows_task(name: str) -> None:
    # COM exposes a numeric state, unlike schtasks' localized text output.
    script = ("$ErrorActionPreference = 'Stop'; $s = New-Object -ComObject Schedule.Service; $s.Connect(); "
              "$t = $s.GetFolder('\\').GetTask($env:SWITCHBOARD_TASK_NAME); "
              "if ($t.State -eq 4) { exit 0 }; exit 1")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                env=dict(powershell_env(), SWITCHBOARD_TASK_NAME=name),
                                capture_output=True, timeout=5, check=False)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise RotatorError("The Windows monitor did not start. Sign in to Windows as the same user running Switchboard, then turn switching on again.")


def windows_task(argv: list[str]) -> bytes:
    try:
        output = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"],
                                         encoding="utf-8", errors="replace", timeout=10)
        user = next(csv.reader(output.splitlines()))[1]
        if not user.startswith("S-1-"):
            raise ValueError("No current user SID")
    except (OSError, ValueError, IndexError, StopIteration, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RotatorError("Could not identify the current Windows user for the background monitor") from exc
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def child(parent, name, text=None, **attributes):
        element = ET.SubElement(parent, "{" + namespace + "}" + name, attributes)
        element.text = text
        return element
    task = ET.Element("{" + namespace + "}Task", {"version": "1.2"})
    triggers = child(task, "Triggers")
    trigger = child(triggers, "LogonTrigger")
    child(trigger, "Enabled", "true")
    child(trigger, "UserId", user)
    principal = child(child(task, "Principals"), "Principal", id="CurrentUser")
    child(principal, "UserId", user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                        ("ExecutionTimeLimit", "PT0S")):
        child(settings, name, value)
    restart = child(settings, "RestartOnFailure")
    child(restart, "Interval", "PT1M")
    child(restart, "Count", "3")
    action = child(child(task, "Actions", Context="CurrentUser"), "Exec")
    executable = Path(argv[0])
    windowless = executable.with_name("pythonw.exe")
    child(action, "Command", str(windowless) if windowless.is_file() else argv[0])
    child(action, "Arguments", subprocess.list2cmdline(argv[1:]))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def remove(config_path: Path, state_dir: Path) -> None:
    if sys.platform.startswith("linux"):
        path = linux_unit_path(config_path)
        if not path.exists():
            return
        run_checked(["systemctl", "--user", "disable", "--now", path.name], "Cannot stop the Linux background monitor")
        path.unlink()
        run_checked(["systemctl", "--user", "daemon-reload"], "Cannot refresh the Linux user services")
    elif sys.platform == "win32":
        path = state_dir / "monitor-task.xml"
        if not path.exists():
            return
        subprocess.run(["schtasks", "/End", "/TN", identity(config_path)], capture_output=True, timeout=20, check=False)
        run_checked(["schtasks", "/Delete", "/TN", identity(config_path), "/F"], "Cannot remove the Windows background monitor")
        path.unlink()
