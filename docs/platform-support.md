# Platform setup and test status

Claude Auto Switch uses the same dashboard on every platform at **http://127.0.0.1:56729/**. Start it through its launcher; opening `ui/index.html` directly does not connect to the application.

## Current availability

**Updated October 13, 2026.** All three platform improvements below are merged into `main` at `cc21d12`. New installations get them through the README quick start. For an existing installation, follow the [upgrade steps](advanced.md#update-an-existing-installation).

| Change | Merged PR |
| --- | --- |
| Linux monitor startup checks, current desktop environment, and process cleanup tests | [Ubuntu/Linux #1](https://github.com/maqboolthoufeeq/claude-auto-switch/pull/1) |
| Windows monitor replacement, startup checks, and automatic child-process cleanup | [Windows #2](https://github.com/maqboolthoufeeq/claude-auto-switch/pull/2) |
| macOS version validation and real LaunchAgent tests on older and Intel runners | [macOS #3](https://github.com/maqboolthoufeeq/claude-auto-switch/pull/3) |

For the exact tested revisions and passing CI links, see the [testing guide](testing.md#verified-platform-checks).

## Requirements and verification

| Platform | Saved switching credentials | Background monitor | Verified | Still to check |
| --- | --- | --- | --- | --- |
| macOS 13+, Apple Silicon or Intel | macOS Keychain | LaunchAgent | Live shared-login rotation on the development Mac; disposable agent startup, replacement, and stop on macOS 14, macOS 15 Intel, and latest runners | Live Claude switching on older/Intel hardware; macOS 13 hardware; existing-chat continuation |
| Ubuntu 20.04+, Debian 10+, compatible Linux desktops | Secret Service, such as GNOME Keyring | systemd user service | Real GNOME Keyring roundtrips; real terminal reads; disposable service startup, restart, and child cleanup on Ubuntu 22.04 | Live Claude switching, existing-chat continuation, and desktop sign-in startup; other distributions |
| Windows 10 1809+ or Windows 11 | Windows Credential Manager | Task Scheduler | Native credential storage, folder permissions, terminal reads, disposable scheduled-task execution, and child cleanup | Live Claude switching, existing-chat continuation, and sign-in startup on desktop Windows; ARM64 hardware |
| WSL | Secret Service inside WSL | systemd user service inside WSL | Uses the Linux adapter; no WSL-specific test completed | WSL setup and the full desktop checklist |

OS minimums follow [Claude Code's system requirements](https://code.claude.com/docs/en/setup#system-requirements). They are installation targets, not a claim that every OS version and architecture has been tested. **macOS 12 and earlier are unsupported by current Claude Code.** Linux without systemd user services or a usable Secret Service cannot enable automatic switching through this implementation.

All platforms need Claude Code and at least two accounts you control. Install Switchboard from npm (`npm install -g cswitch`, which needs Node.js 18+ and either [uv](https://docs.astral.sh/uv/getting-started/installation/) or Python 3.9+), or from a Git checkout with Git and uv, which manages Python for you. Use the same OS user for Switchboard, its credential store, and your Claude terminals. The [README](../README.md#2-connect-your-accounts) explains connecting and preparing accounts.

## macOS

1. Use macOS 13 or newer. Intel and Apple Silicon are targets; uv selects the Python architecture for your machine.
2. Install the requirements above and follow the [README quick start](../README.md#1-download-and-open-switchboard).
3. Next time, run `cswitch`, or open **Open Switchboard.command** in a Git checkout.
4. Allow macOS Keychain access if requested. A locked Keychain or reset permissions may prompt again; Switchboard cannot guarantee lifetime permission without further OS prompts.

New installations do not need the Swift helper. It is retained only for recovering older Switchboard Keychain entries; see [migration](advanced.md#shared-login-controls).

The launcher checks the OS version before starting the dashboard or resolving Claude and gives an update message on unsupported Macs. CI tests real LaunchAgent startup, replacement, and removal on macOS 14 / Python 3.9 and macOS 15 Intel / Python 3.14, as well as latest Mac runners. macOS 13 needs a physical-machine check because its hosted runner is retired.

## Ubuntu and Linux desktops

1. Install Claude Code, then Node.js and uv for the npm route, or uv and Git for a checkout.
2. Use a normal logged-in desktop session with systemd user services. If the desktop is missing these packages, install them on Ubuntu:

   ```sh
   sudo apt install gnome-keyring dbus-user-session zenity
   ```

3. Unlock the desktop keyring. Switchboard does not store its password. Another compatible Secret Service provider can replace GNOME Keyring; a plaintext backend cannot.
4. Follow the [README quick start](../README.md#1-download-and-open-switchboard). Next time, run `cswitch`, or `./Open\ Switchboard.sh` from the project folder of a Git checkout.
5. Prepare your accounts and turn on switching through the dashboard.

Zenity or KDialog supplies the optional folder picker. You can type the project path if neither is installed. Installing system packages may require administrator permission; running Switchboard and its monitor uses your ordinary user account.

The Linux monitor imports current desktop session addresses into the systemd user manager instead of saving them in the service file. It checks that the service is active before enabling startup. Stopping the service also stops its temporary usage readers. Real startup, replacement, and cleanup were tested with a disposable Ubuntu 22.04 service; this does not establish desktop logout/login behavior.

Headless servers, non-systemd Linux, and WSL are not verified setups. Do not disable credential-store encryption to work around a missing desktop session.

## Native Windows

1. Use Windows 10 1809+ or Windows 11 as your normal signed-in user.
2. Install Claude Code with its [native Windows installer](https://code.claude.com/docs/en/setup#install-on-native-windows). Switchboard requires **`claude.exe`**; batch launchers such as `claude.cmd` cannot safely preserve all task arguments.
3. With Node.js and uv installed, run `npm install -g cswitch`, then `cswitch`. For a Git checkout instead, install uv and Git, clone the repository as described in the README, open PowerShell in the cloned folder, and run:

   ```powershell
   uv sync --python 3.14
   uv run launch.py
   ```

4. Connect and prepare accounts in the dashboard. Next time, run `cswitch` (npm), or open **Open Switchboard.cmd** in the checkout.

The current terminal dependency provides x64 and ARM64 wheels with Python 3.10+. Python 3.9 uses the older x64 terminal dependency. ARM64 hardware has not been tested.

Turning on switching creates a Task Scheduler task for the current user. It is configured to start immediately and at sign-in, with ordinary user permissions and **no saved Windows password**. It runs only while that user is signed in. Turning switching off removes the task. Organization policies can restrict task creation; Switchboard reports registration failures.

The Windows monitor stops an older monitor before replacing it and confirms that the new task is running. A private Windows job object cleans up temporary readers when the monitor is terminated. Native CI verifies execution and cleanup using harmless processes. Actual desktop sign-in startup still needs the checklist in the testing guide.

The folder picker and private-folder permissions use built-in Windows PowerShell. Keep native Windows and WSL installations separate: each has its own active Claude login and credential store.

## Before relying on automatic switching

Complete the [desktop test checklist](testing.md#1-check-the-dashboard), especially the two-terminal and sign-in startup checks. A dashboard route change verifies the shared login changed; it does not establish that every existing chat adopted that login.

Switchboard uses each account's configured **current session** threshold. The reported-limit fallback is on by default and can be turned off. Weekly usage is not used for switching. A failed percentage check reports an error rather than inventing a usage value.

Credentials, profiles, logs, and private dashboard links stay on your computer and are excluded from repository commits. Sign in separately on each machine. Claude manages its own active credential files on Linux and Windows; Switchboard's saved switching copies use the OS credential store. See [privacy and limits](../README.md#privacy-and-limits).
