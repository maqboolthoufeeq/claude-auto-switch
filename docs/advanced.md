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

## Shared login controls — macOS

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

`setup` imports signed-in profiles into macOS Keychain. Turn switching off before preparing accounts again. `enable` installs a background monitor and a Claude `StopFailure` hook in `~/.claude/settings.json`, preserving existing hooks and saving a settings backup in `.shared-rotator/`. New Claude chats load the hook. `disable` removes the monitor and Switchboard's hook; the current shared login stays in place.

The monitor reads only the active account's current session percentage. Checks use a short-lived Claude session in safe mode and send `/usage`, without a model prompt. Readings must belong to the current login and be no more than 60 seconds old. The percentage comes from Claude's local cache after opening the usage display; this internal format can change.

The failure hook runs after a failed turn. It switches on a reported rate limit when fallback is enabled, but cannot replay the failed message. Events from concurrent terminals are serialized, and near-simultaneous limit events are treated as one switch. The target email and organization are verified before recording success.

Claude cloud sessions are not controlled. Account-linked Remote Control connections may need to reconnect after a switch. Terminal chats using a separate `CLAUDE_CONFIG_DIR`, an API key, or a fixed `CLAUDE_CODE_OAUTH_TOKEN` do not follow the shared login.

For older saved Keychain items only:

```sh
uv run shared_mode.py migrate
```

This moves credentials to items owned by Apple's signed Keychain utility and removes obsolete copies. The old Swift helper may require Xcode command line tools during migration. Normal switches use the signed utility, with credential data passed through standard input and output rather than command arguments.

## Guided tasks — macOS or Linux

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

Python 3.9 or newer is required and managed by uv. There are no third-party Python dependencies.

```sh
uv sync
uv run python -B -m unittest discover -s tests -v
node --check ui/app.js
```

Tests simulate Claude; they do not contact Anthropic or use your accounts. The runner removes inherited API-key and provider-routing variables and verifies a `claude.ai` login before using a profile. It uses Claude's `auto` permission mode and preserves organization policies.
