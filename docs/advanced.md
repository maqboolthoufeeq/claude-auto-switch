# Commands and advanced settings

For the dashboard setup, start with the [README](../README.md).

## Update an existing installation

With npm, run `npm install -g cswitch@latest`, then start `cswitch`. Reopening the dashboard points the background monitor and the limit hook at the updated copy. Your data in `~/.cswitch` is kept.

For a Git checkout:

1. Finish or pause any guided task. Turn off automatic switching in the dashboard, then close the dashboard launcher.
2. Open a terminal in the existing Switchboard project folder and run:

   ```sh
   git pull --ff-only
   uv sync
   uv run launch.py
   ```

   On native Windows, use `uv sync --python 3.14` for the install step.

3. Check the account route, then turn automatic switching back on. Linux needs an unlocked desktop credential store; Windows needs the same signed-in user.
4. Confirm that the session usage reading updates. Follow the [background and startup checks](testing.md#4-test-fallback-and-background-operation) before relying on the monitor.

Updating in the same folder keeps your local configuration and profiles. Do not delete them or copy credentials into Git. If Git reports local source changes, review those changes before updating; do not discard them to force the update. Continue using port **56729**.

## Account profiles

`rotator.json` is your local configuration, kept in the data folder (`~/.cswitch` for npm installs, or `CSWITCH_HOME`; the project folder for a Git checkout). Create it with `cswitch init` or `uv run rotator.py init`. It starts with five profiles; the dashboard can add or remove accounts.

Each `config_dir`, such as `profile/account-1`, must be a separate directory. Relative paths are resolved from the folder containing `rotator.json`. An optional `email` prefills the browser sign-in. Different profiles may share an email when Claude reports different organizations.

Sign in or check status from Terminal (`cswitch login account-1` and `cswitch accounts` do the same for npm installs):

```sh
uv run rotator.py login account-1
uv run rotator.py login account-2
uv run rotator.py status
```

Claude handles authentication and creates the profile files. Do not store account passwords in the project.

## Shared login controls

These commands match the dashboard controls. With npm, use `cswitch setup`, `cswitch switch account-1`, `cswitch enable`, `cswitch status`, `cswitch fallback on|off`, and `cswitch disable`.

```sh
uv run shared_mode.py setup
uv run shared_mode.py switch account-1
uv run shared_mode.py enable
uv run shared_mode.py status
uv run shared_mode.py fallback off
uv run shared_mode.py fallback on
uv run shared_mode.py disable
```

`setup` imports signed-in profiles into the protected OS credential store. Turn switching off before preparing accounts again. `enable` installs a background monitor and a Claude `StopFailure` hook in `~/.claude/settings.json`, preserving existing hooks and saving a settings backup in `.shared-rotator/`. New Claude chats load the hook. The hook only starts the switch in a separate background process and returns, so a Claude session that exits right after a limit error cannot interrupt it. `disable` removes the monitor and Switchboard's hook; the current shared login stays in place.

The monitor uses a macOS LaunchAgent, a Linux systemd user service, or a Windows Task Scheduler task for the current interactive user. It requests no administrator elevation. Linux Secret Service must be available in that user's D-Bus session. Windows hooks use explicitly quoted PowerShell commands; macOS and Linux use the system shell.

The platform improvements are merged into `main`; see [availability and verification](platform-support.md#current-availability). Background behavior:

- **Linux:** import only current desktop session variables into the systemd manager, keep those addresses out of the unit file, use `Type=exec`, confirm the service is active before enabling startup, and stop its full process group when disabled.
- **Windows:** stop an older scheduled monitor before replacement, check its numeric running state through Task Scheduler, and keep descendants in a private kill-on-close job object. The job contains monitor descendants, not your existing Claude terminals.
- **macOS:** check the macOS 13 minimum before launch and Claude resolution; test actual LaunchAgent replacement and removal on older/Intel runners.

The generated service/task names depend on the resolved configuration path. Only one monitor runs per user: turning switching on, or reopening the dashboard while it is on, retires monitors registered by another install or folder. Moving the application or replacing its Python environment can invalidate a registered monitor. Turn switching off before moving it. In the new location, run `uv sync`, sign in and prepare accounts again, then enable switching. Reopening the launcher refreshes the monitor registration when switching is enabled. Sign in separately on each machine instead of copying credential/profile folders.

The monitor reads only the active account's current session percentage. Checks use a short-lived Claude session in safe mode and send `/usage`, without a model prompt. The terminal display is rendered with pyte so a refreshed percentage replaces earlier text. The reader waits for refreshing to finish and verifies the shared login before switching. When it is done it sends `/exit` and waits for Claude to quit, including any login refresh in progress, because a Claude process killed mid-refresh can leave the shared login with a token the server has already replaced. Monitor restarts wait for a running check. When `/usage` shows when the session resets, an account that reached its threshold rests until then instead of a flat five hours. If the shared login was changed to another prepared account outside Switchboard, the monitor follows it. It does not depend on Claude's local usage cache, which can remain stale. Claude's display format can change.

The failure hook runs after a failed turn. It switches on a reported rate limit when fallback is enabled, but cannot replay the failed message. Events from concurrent terminals are serialized. Limit reports that arrive within 90 seconds of any switch are treated as reports about the previous login, because open chats and workflow agents keep using it for a while. The target email and organization are verified before recording success.

If Claude rejects a saved login, that account is marked **Needs sign-in** and skipped on subsequent automatic checks. Turn switching off, sign in again to that profile, update prepared accounts, then turn switching on. If no prepared account is available, the current login stays in place and the dashboard explains what needs attention.

Claude cloud sessions are not controlled. Account-linked Remote Control connections may need to reconnect after a switch. Terminal chats using a separate `CLAUDE_CONFIG_DIR`, an API key, or a fixed `CLAUDE_CODE_OAUTH_TOKEN` do not follow the shared login.

For older saved macOS Keychain items only:

```sh
uv run shared_mode.py migrate
```

This repairs older saved credentials, moves them to items owned by Apple's signed Keychain utility, and removes obsolete copies. Long credentials are split into small Keychain items, verified after writing, and checked against a checksum when read. The old Swift helper may require Xcode command line tools during migration. Normal switches use the signed utility, with credential data passed through standard input and output rather than command arguments.

## Guided tasks

Turn off shared switching before using the separate-profile task runner. The two modes refresh copies of the same OAuth credentials, so running them together can leave a login needing sign-in again.

```sh
uv run rotator.py run --project /absolute/path/to/project "Implement the feature and run its tests"
uv run rotator.py resume --project /absolute/path/to/project
```

The runner saves its handoff and private logs in the code project's `.claude-rotator/` folder. It adds that folder and any in-project profiles to the project's Git ignore rules. A file lock prevents simultaneous guided runs in one project.

Within a profile, Claude resumes its own session. Across profiles, the next run uses the saved handoff and inspects the current files. Other errors pause the task for review. `max_runs` in `rotator.json` limits automatic continuation attempts; increase it before resuming if needed.

## Custom usage checks

Each account's `threshold_percent` defaults to 95. In shared mode, a configured `usage_command` replaces the built-in session reader for the active account. In guided mode, it checks usage before each run; otherwise rotation happens at a confirmed usage limit.

```json
"usage_command": ["/absolute/path/to/usage-check", "--account", "account-1"]
```

The command must print a number from 0 to 100, or JSON such as `{"used_percent": 94.5}`. It receives `CLAUDE_CONFIG_DIR`, `CLAUDE_ROTATOR_ACCOUNT`, and `CLAUDE_ROTATOR_PROJECT` (the data folder in shared mode, the task's project in guided mode) in its environment. Use current session usage for this value. Failed checks do not invent a percentage.

## Development

Python 3.9 or newer is required and managed by uv. `uv sync` installs pyte, keyring on Linux/Windows, and pywinpty on Windows. Python 3.10+ is recommended on native Windows for current terminal wheels, including ARM64. Python 3.9 uses the older pywinpty 2 release.

```sh
uv sync --group tests
uv run --group tests python -B -m unittest discover -s tests -v
node --check ui/app.js
node --check bin/cswitch.js
npm pack --dry-run
```

The npm package `cswitch` contains the Python sources and a small Node launcher, `bin/cswitch.js`. It runs them with `uv run --frozen` (environment in `$CSWITCH_HOME/venv`) or, without uv, a plain Python virtual environment in `$CSWITCH_HOME/venv-pip`. `npx cswitch` works for one-off commands, but automatic switching needs an installed copy because the hook and monitor run the package files later.

Tests use fake Claude responses and disposable data; they do not contact Anthropic or switch your accounts. Native platform probes do create and remove temporary credential entries, tasks, or services. The opt-in flags, exact verified revisions, CI matrix, and remaining desktop checks are documented in the [testing guide](testing.md#run-the-automated-tests).

Create a separate feature branch and PR for each change. Commit source and documentation only. Do not add `rotator.json`, profile folders, `.shared-rotator/`, credentials, `.env`, or logs.

The optional `tests` dependency group installs PyPA distlib only on Windows to generate real executable test launchers. Batch-file stubs cannot preserve all Claude arguments, especially multiline prompts; runtime installations do not need this test dependency.

The runner removes inherited API-key and provider-routing variables and verifies a `claude.ai` login before using a profile. It uses Claude's `auto` permission mode and preserves organization policies. On Windows, private runtime/profile folders receive a protected ACL allowing the current user and SYSTEM; Unix folders use mode 0700. Do not place profiles on shared folders or copy the private dashboard link to other users.
