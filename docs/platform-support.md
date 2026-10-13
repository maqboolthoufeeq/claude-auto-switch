# Platform setup and test status

The same dashboard and switching logic run on each platform, always at **http://127.0.0.1:56729/**. The adapters use each operating system's credential store, terminal, and background service.

| Platform | Saved switching credentials | Background monitor | Verification |
| --- | --- | --- | --- |
| macOS 13+, Apple Silicon or Intel | macOS Keychain | LaunchAgent | Real shared-login rotation verified on the current Apple Silicon Mac; older versions and Intel hardware still need live checks |
| Ubuntu 20.04+, Debian 10+, other Linux desktops | Secret Service, such as GNOME Keyring | systemd user service | Preview: automated suite, real keyring, and terminal checks; real Claude login and desktop service checks remain |
| Windows 10 1809+, Windows 11 | Windows Credential Manager | Task Scheduler, current logged-in user | Preview: Windows CI includes native credential and terminal checks; real Claude login and desktop startup checks remain |
| WSL | Linux Secret Service inside WSL | systemd user service inside WSL | Same Linux implementation; WSL desktop/keyring setup has not been verified |

These OS minimums follow [Claude Code's published requirements](https://code.claude.com/docs/en/setup#system-requirements). Current Claude Code does not support macOS before 13. Switchboard cannot make those older versions compatible. New macOS installations do not require the Swift helper; it is retained only to recover older Switchboard Keychain entries.

## macOS

Follow the [README](../README.md). Use **Open Switchboard.command** after the first `uv sync`. The same implementation targets Intel and Apple Silicon; uv selects the matching Python architecture.

## Ubuntu and Linux desktops

Install Claude Code, uv, and Git, then follow the README. A working **systemd user session** and **unlocked Secret Service** are required for automatic switching. For an Ubuntu desktop missing these packages:

```sh
sudo apt install gnome-keyring dbus-user-session zenity
```

Sign in to your desktop normally and unlock its keyring. The monitor imports your current desktop session environment into systemd without saving session addresses in its service file. It confirms the service is active before enabling startup, and stopping it also stops its temporary usage readers. Switchboard does not save the keyring password. A compatible Secret Service provider can replace GNOME Keyring; an arbitrary or plaintext Python keyring backend cannot.

Start with `uv run launch.py`, then use **Open Switchboard.sh**. Zenity or KDialog supplies the optional folder picker; you can also type a project path.

On a headless server or Linux without systemd user services, automatic switching cannot be enabled through this implementation. Do not disable keyring encryption to work around that limitation.

## Native Windows

Use Claude Code's native Windows installer, which supplies **claude.exe**, and install uv and Git as your normal Windows user. Switchboard requires this executable to preserve multiline task arguments safely. Open PowerShell in the cloned project:

```powershell
uv sync --python 3.14
uv run launch.py
```

Then use **Open Switchboard.cmd**. The Windows terminal dependency has current x64 and ARM64 wheels for Python 3.10+. Python 3.9 is retained for compatibility with the older x64 terminal dependency.

Turning on automatic switching creates a Task Scheduler task for your current user. It starts immediately and at your next sign-in, with ordinary user permissions and no saved Windows password. It watches only while that user is signed in. Turning switching off removes the task. Starting switching first replaces any previous monitor and confirms that Task Scheduler reports it running. A private Windows job object stops the monitor’s temporary usage readers if its task is terminated. Your organization may restrict task registration; Switchboard reports that failure instead of silently claiming monitoring is active.

Windows CI checks actual execution of a disposable Task Scheduler task and confirms that forced monitor exit also terminates its child process. These probes never use Claude accounts.

The folder picker and private-folder access controls use the built-in Windows PowerShell. The dashboard, profiles, and Claude must run under the same user. Keep native Windows and WSL installations separate: they have different active Claude logins and credential stores.

## Verify a platform before relying on it

Follow [testing.md](testing.md), including the two-terminal continuation test. A passed simulated test or a changed route marker alone does not verify that an already-open Claude chat adopted the new account.

All platforms keep account names, per-account session thresholds, manual switching, and the user-controlled limit fallback. Weekly usage is still not used for switching.

Ubuntu 22.04 CI includes a disposable systemd user service test for actual startup, replacement, and process cleanup. It does not sign in to Claude or simulate a desktop login.
