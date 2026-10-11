# claude-auto-switch

This local tool continues a coding task across Claude Code Team logins that you control. Each login gets its own Claude Code profile. When a profile reaches a subscription usage limit, the tool starts a new Claude Code run with the next profile and passes the saved task handoff. The separate-profile task runner uses Claude Code's own browser login. An optional shared-login mode for independent terminals stores credential copies in macOS Keychain and uses Claude's browserless login flow.

## Open the dashboard

On macOS, double-click `Open Switchboard.command` in this folder. Or start it from Terminal:

```sh
uv run --offline rotator_ui.py
```

It opens a private dashboard at **http://127.0.0.1:56729/**. Opening the launcher again brings back the running dashboard on that same address. The first card shows the account route: connected profiles, accounts needing sign-in, and the current shared login. Select an account in the route, or its **i** button in the account list, to see its profile ID, connection details, and recent activity. The details window also has **Log out** and **Delete account**. Deleting removes the account from Switchboard and signs out its profile; its local profile folder and chat history remain on your Mac. Switch to another shared account first if the account you want to remove is currently active.

The automatic switching controls are below the route. To have Switchboard start Claude for a specific project, open **Run a guided task**, choose the project folder, describe the task, and click **Start task**. That tab also shows progress and saved task controls. Keep the launcher window open while a guided task runs. Switchboard records sign-ins performed through its dashboard and when it first observes an existing connected profile. Claude does not provide the original login time for profiles that were already signed in.

If you see **Switchboard needs to be reopened**, an older launcher is still serving the browser tab. Close the Switchboard Terminal window and double-click `Open Switchboard.command` again. Reloading that browser tab alone does not update the launcher.

The dashboard runs only on `127.0.0.1` and requires the one-time link printed by the launcher. There is no cloud server or account password store. You can add accounts and sign in from the dashboard; Claude Code still handles authentication in your browser. On macOS, **Choose folder** opens the system folder picker. You can also paste the folder path.

Open **Account settings** on any account to give it a display name, such as “Research seat.” The underlying profile ID and login stay the same, and saved task history continues to refer to that stable ID.

## Shared login for independent terminals (macOS)

The dashboard opens on **Automatic switching**. Use this for Claude chats you open yourself in different local project folders. All those terminals must use plain `claude` with the same default `~/.claude` login. A terminal launched with its own `CLAUDE_CONFIG_DIR`, an API key, or a fixed `CLAUDE_CODE_OAUTH_TOKEN` does not follow the shared login.

Saved account credentials live in macOS Keychain. Switchboard uses Apple's signed `security` utility to read and update them; the values travel through standard input and output, never process arguments. An older installation may need the Swift compiler once to read and move its previously saved credentials. Use **Update saved account access** in the dashboard if it appears. macOS may ask to approve access to those older items during the move; routine switches then use the new items.

1. Sign in to the accounts you want to rotate in their `profile/account-n` folders as described below. Accounts can share an email if Claude reports different organizations. Unsigned profiles are skipped.
2. Open Switchboard and click **Prepare accounts**. Switchboard reads each profile's Claude credential and puts a private copy in macOS Keychain. It saves no passwords or tokens in this project.
3. If the dashboard says the current shared account is “not matched,” choose one of your accounts under **Switch manually** and click **Switch account**. This changes the default Claude login immediately; other local Claude terminals using that login may use the new account on their next request.
4. Click **Turn on automatic switching**. Switchboard checks the active account's **Current session** usage about every 30 seconds using Claude Code's interactive `/usage` screen. At that account's saved percentage, it signs in to the next prepared account. The monitor continues after the dashboard closes. The check uses safe mode and sends no model prompt.
5. **Switch at a reported limit too** is on by default. It installs a `StopFailure` rate-limit hook in `~/.claude/settings.json`, preserving existing settings and saving a backup at `.shared-rotator/settings-before-shared-login.json`. This fallback can switch when the percentage check is unavailable or a limit arrives between checks. You can turn it off in the dashboard. Open new Claude terminal chats after enabling switching so they load the hook. Click **Turn off switching** to remove the hook and monitor.

Claude's failure hook runs after the failed turn; it cannot replay that turn. The original chat stays open, and you can retry or send the next message after the switch. If two terminals fail at nearly the same time, Switchboard treats their events as one switch. It verifies the new account's email and organization before recording a successful switch. The shared mode does not control Claude cloud sessions. Remote Control sessions still run locally, though account-linked remote connections may need to reconnect after a login change.

For Team seats, Claude Code shows a session percentage in `/usage`, but does not document that percentage in the status-line data. Switchboard reads the interactive screen and uses only **Current session**. Weekly usage is not used. If Claude changes that screen or requires interactive setup in this project folder, the dashboard reports that the percentage is unavailable; the confirmed-limit fallback remains available when enabled. A configured `usage_command` takes priority over the built-in screen check for the active account.

The per-account profiles and shared mode use the same underlying OAuth accounts. Avoid running the separate-profile task runner at the same time as shared mode: refreshing one copy of a credential can leave the other copy needing sign-in again. If an imported login expires or Claude changes its Keychain format, turn shared mode off, sign in to that profile again, then prepare the accounts again. Switchboard never stores your account passwords.

Terminal commands for the same controls:

```sh
uv run shared_mode.py setup
uv run shared_mode.py switch account-1
uv run shared_mode.py enable
uv run shared_mode.py status
uv run shared_mode.py migrate  # Only for older saved Keychain items
uv run shared_mode.py disable
```

## Set up

Requires macOS or Linux, [uv](https://docs.astral.sh/uv/), Git, and Claude Code 2.1.259+ installed on your path. The uv project requires Python 3.9 or newer and has no third-party dependencies. This tool uses Claude Code's non-interactive run mode and `auto` permission mode. It does not turn on bypass permissions.

The profiles listed in `rotator.json` live in this tool's `profile/` folder. Each `profile/account-n` is a **directory**, not a file to fill in. Claude Code creates and manages its own settings and sign-in data there. Do not put passwords in `.env` or in the profile folders. The `profile/` folder and local `rotator.json` are ignored by Git.

1. Open `rotator.json`. It lists each account and its `config_dir`, such as `profile/account-1`. You may fill in each `email` to prefill that login in the browser; leaving it blank is fine. Keep each `config_dir` distinct. Relative paths are resolved from the folder containing `rotator.json`.
2. From this tool's folder, prepare uv and sign in to each account, one at a time:

   ```sh
   uv sync
   uv run rotator.py login account-1
   uv run rotator.py login account-2
   uv run rotator.py login account-3
   uv run rotator.py login account-4
   uv run rotator.py login account-5
   uv run rotator.py login account-6  # If listed in rotator.json
   ```

   Each command opens Anthropic's normal browser login. Check that the correct Team account and organization are selected before finishing each login. If the browser is already signed in to another account, switch accounts there. The command creates its profile directory automatically.

3. Verify all configured logins:

   ```sh
   uv run rotator.py status
   ```

   Each account should say `signed in`. If one does not, repeat its `login` command. For a new installation without `rotator.json`, run `uv run rotator.py init` first.

## Run a task

```sh
uv run rotator.py run --project /absolute/path/to/your/project "Implement the feature and run its tests"
```

The tool saves a task handoff and private logs in `<project>/.claude-rotator/`. In a Git project it adds `.claude-rotator/` and any profile folders inside that project to its `.gitignore`. If the run pauses, continue it with:

```sh
uv run rotator.py resume --project /absolute/path/to/your/project
```

Use one rotator run per project at a time. A file lock prevents simultaneous runs in the same project. A new `run` is allowed after the previous task is complete. `max_runs` in `rotator.json` limits automatic continuation attempts; increase it before `resume` if needed.

## Usage thresholds

Each account has a `threshold_percent` setting, initially 95. Shared-login mode checks the active account's current session usage as described above. The separate-profile guided task runner still uses a confirmed usage-limit failure unless you supply a `usage_command` for that profile. It switches profiles at the next run boundary and continues from the handoff and current files. It cannot move an in-progress model response to another account.

If you have an approved command that reports a Team seat's usage percentage, set that account's `usage_command` to an argument array, for example:

```json
"usage_command": ["/absolute/path/to/usage-check", "--account", "account-1"]
```

The command must print a number from 0 to 100, or JSON such as `{"used_percent": 94.5}`. It receives `CLAUDE_CONFIG_DIR`, `CLAUDE_ROTATOR_ACCOUNT`, and `CLAUDE_ROTATOR_PROJECT` in its environment. The rotator checks it before each Claude run and skips a profile when it meets that profile's threshold. A failed or unavailable check falls back to switching on a confirmed limit. The tool does not scrape Anthropic's site or call private endpoints.

## How continuation works

Claude Code resumes its own session while it stays on one profile. Across profiles, session history is separate, so the new run reads the handoff and inspects the project's current files. The prompt asks Claude to update the handoff after each substantial step. A limit can still interrupt work before that update; the next run is told to inspect the actual files. Other errors pause the task and leave logs for review.

The tool removes inherited API-key and provider-routing variables when it launches a Team profile, and checks that Claude Code reports a `claude.ai` login. This helps avoid charging a Console API key by accident. Your organization's Claude Code settings and permissions still apply.

## Test

```sh
uv run python -B -m unittest discover -s tests -v
```

The tests use a simulated Claude command; they do not contact Anthropic or use any of your accounts.
