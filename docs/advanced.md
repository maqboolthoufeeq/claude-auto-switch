# Commands and advanced settings

For the dashboard setup, start with the [README](../README.md).

## Account profiles

`rotator.json` is your local configuration. Create it with `uv run rotator.py init`. It starts with five profiles; the dashboard can add or remove accounts.

Each `config_dir`, such as `profile/account-1`, must be a separate directory. Relative paths are resolved from the folder containing `rotator.json`. An optional `email` prefills the browser sign-in. Different profiles may share an email when Claude reports different organizations.

Sign in or check status from Terminal:

```sh
uv run rotator.py login account-1
uv run rotator.py login account-2
uv run rotator.py status
```

Claude handles authentication and creates the profile files. Do not store account passwords in the project.

## Shared login controls

These commands match the dashboard controls:

```sh
uv run shared_mode.py setup
uv run shared_mode.py switch account-1
uv run shared_mode.py enable
uv run shared_mode.py status
uv run shared_mode.py fallback off
uv run shared_mode.py fallback on
uv run shared_mode.py disable
```

`setup` imports signed-in profiles into the protected OS credential store. Turn switching off before preparing accounts again. `enable` installs a background monitor and a Claude `StopFailure` hook in `~/.claude/settings.json`, preserving existing hooks and saving a settings backup in `.shared-rotator/`. New Claude chats load the hook. `disable` removes the monitor and Switchboard's hook; the current shared login stays in place.

The monitor uses a macOS LaunchAgent, a Linux systemd user service, or a Windows Task Scheduler task for the current interactive user. It requests no administrator elevation. Linux Secret Service must be available in that user's D-Bus session. Windows hooks use explicitly quoted PowerShell commands; macOS and Linux use the system shell.

For the startup and cleanup changes in the platform feature branches, see [current availability](platform-support.md#current-availability). Their implementations:

- **Linux:** import only current desktop session variables into the systemd manager, keep those addresses out of the unit file, use `Type=exec`, confirm the service is active before enabling startup, and stop its full process group when disabled.
- **Windows:** stop an older scheduled monitor before replacement, check its numeric running state through Task Scheduler, and keep descendants in a private kill-on-close job object. The job contains monitor descendants, not your existing Claude terminals.
- **macOS:** check the macOS 13 minimum before launch and Claude resolution; test actual LaunchAgent replacement and removal on older/Intel runners.

The generated service/task names depend on the resolved configuration path. Moving the application or replacing its Python environment can invalidate a registered monitor. Turn switching off before moving it. In the new location, run `uv sync`, sign in and prepare accounts again, then enable switching. Reopening the launcher refreshes the monitor registration when switching is enabled. Sign in separately on each machine instead of copying credential/profile folders.

The monitor reads only the active account's current session percentage. Checks use a short-lived Claude session in safe mode and send `/usage`, without a model prompt. The terminal display is rendered with pyte so a refreshed percentage replaces earlier text. The reader waits for refreshing to finish and verifies the shared login before switching. It does not depend on Claude's local usage cache, which can remain stale. Claude's display format can change.

The failure hook runs after a failed turn. It switches on a reported rate limit when fallback is enabled, but cannot replay the failed message. Events from concurrent terminals are serialized, and near-simultaneous limit events are treated as one switch. The target email and organization are verified before recording success.

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

The command must print a number from 0 to 100, or JSON such as `{"used_percent": 94.5}`. It receives `CLAUDE_CONFIG_DIR`, `CLAUDE_ROTATOR_ACCOUNT`, and `CLAUDE_ROTATOR_PROJECT` in its environment. Use current session usage for this value. Failed checks do not invent a percentage.

## Development

Python 3.9 or newer is required and managed by uv. `uv sync` installs pyte, keyring on Linux/Windows, and pywinpty on Windows. Python 3.10+ is recommended on native Windows for current terminal wheels, including ARM64. Python 3.9 uses the older pywinpty 2 release.

```sh
uv sync --group tests
uv run --group tests python -B -m unittest discover -s tests -v
node --check ui/app.js
```

Tests use fake Claude responses and disposable data; they do not contact Anthropic or switch your accounts. Native platform probes do create and remove temporary credential entries, tasks, or services. The opt-in flags, exact verified revisions, CI matrix, and remaining desktop checks are documented in the [testing guide](testing.md#run-the-automated-tests).

Create a separate feature branch and PR for each change. Commit source and documentation only. Do not add `rotator.json`, profile folders, `.shared-rotator/`, credentials, `.env`, or logs.

The optional `tests` dependency group installs PyPA distlib only on Windows to generate real executable test launchers. Batch-file stubs cannot preserve all Claude arguments, especially multiline prompts; runtime installations do not need this test dependency.

The runner removes inherited API-key and provider-routing variables and verifies a `claude.ai` login before using a profile. It uses Claude's `auto` permission mode and preserves organization policies. On Windows, private runtime/profile folders receive a protected ACL allowing the current user and SYSTEM; Unix folders use mode 0700. Do not place profiles on shared folders or copy the private dashboard link to other users.
