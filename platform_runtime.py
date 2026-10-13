"""Small OS-specific helpers shared by the runner and dashboard."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class RotatorError(Exception):
    pass


def powershell_env() -> dict[str, str]:
    # Python inherits PowerShell 7 module paths, which Windows PowerShell 5.1 cannot load.
    return {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        path.chmod(0o700)
        return
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$acl = New-Object System.Security.AccessControl.DirectorySecurity; "
        "$acl.SetAccessRuleProtection($true, $false); "
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        "$acl.SetOwner($user); "
        "foreach ($sid in @($user, [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) { "
        "$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', "
        "'ContainerInherit, ObjectInherit', 'None', 'Allow'); $acl.AddAccessRule($rule) }; "
        "Set-Acl -LiteralPath $env:SWITCHBOARD_PRIVATE_PATH -AclObject $acl"
    )
    env = dict(powershell_env(), SWITCHBOARD_PRIVATE_PATH=str(path.resolve()))
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                env=env, capture_output=True, timeout=20, check=False)
        if result.returncode != 0:
            raise OSError("Could not restrict folder access")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotatorError("Could not protect the private Switchboard folder. Choose a folder owned by your Windows user account.") from exc


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError("The file is already locked") from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def process_options() -> dict:
    return ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})


def interrupt_process(process: subprocess.Popen, force: bool = False) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        if not force:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except OSError:
                pass  # A background Python process may not have a console.
        kill_windows_tree(process.pid)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        except ProcessLookupError:
            pass


def kill_windows_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=15, check=False)


def choose_folder() -> str:
    if sys.platform == "darwin":
        command = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose the code project")']
    elif sys.platform == "win32":
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command",
                   "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                   "Add-Type -AssemblyName System.Windows.Forms; "
                   "$picker = New-Object System.Windows.Forms.FolderBrowserDialog; "
                   "$picker.Description = 'Choose the code project'; "
                   "if ($picker.ShowDialog() -eq 'OK') { $picker.SelectedPath }"]
    elif sys.platform.startswith("linux"):
        import shutil
        if shutil.which("zenity"):
            command = ["zenity", "--file-selection", "--directory", "--title=Choose the code project"]
        elif shutil.which("kdialog"):
            command = ["kdialog", "--getexistingdirectory", str(Path.home())]
        else:
            raise RotatorError("Enter the project folder path, or install zenity or kdialog to use the folder picker")
    else:
        raise RotatorError("Enter the project folder path on this computer")
    result = subprocess.run(command, env=powershell_env() if sys.platform == "win32" else None,
                            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RotatorError("No folder was selected")
    return result.stdout.strip()
