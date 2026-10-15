# Test Switchboard

Use the checks below on each operating system. Start through its launcher and keep the dashboard on port **56729**. Follow [platform setup](platform-support.md) first.

## Verified platform checks

**Evidence recorded October 13, 2026.** The three platform PRs are merged into `main`. The combined revision `cc21d12` passed all nine CI jobs. Each link below identifies the tested revision; the earlier feature runs show the individual changes.

| Code tested | Revision | CI result | What it adds |
| --- | --- | --- | --- |
| Before platform improvements | `33b31a9` | [6 jobs passed](https://github.com/maqboolthoufeeq/claude-auto-switch/actions/runs/38385324784) | Ubuntu, macOS, and Windows on Python 3.9 and 3.14; native terminal and lock tests; Windows credential store, private folders, and task registration |
| Ubuntu/Linux PR #1 | `263dfbf` | [7 jobs passed](https://github.com/maqboolthoufeeq/claude-auto-switch/actions/runs/38388065937) | Real disposable systemd user service startup, replacement, and complete process cleanup on Ubuntu 22.04 |
| Windows PR #2 | `e8292fd` | [6 jobs passed](https://github.com/maqboolthoufeeq/claude-auto-switch/actions/runs/38388300818) | Real scheduled-task execution and child cleanup after task termination; terminal reading inside the monitor's job object |
| macOS PR #3 | `0c34482` | [8 jobs passed](https://github.com/maqboolthoufeeq/claude-auto-switch/actions/runs/38388381966) | Real LaunchAgent startup, replacement, and stop; macOS 14 / Python 3.9 and macOS 15 Intel / Python 3.14 |
| Merged `main` | `cc21d12` | [9 jobs passed](https://github.com/maqboolthoufeeq/claude-auto-switch/actions/runs/38392172818) | All three improvements together, including the separate Ubuntu 22.04 service job and older/Intel Mac runners |

The three PRs also combined without merge conflicts. A disposable combined source snapshot ran 55 tests on the development Mac: **50 passed and 5 platform-specific checks were skipped**. This local integration check preceded the passing merged-main CI run linked above.

Additional checks:

- A live Claude `/usage` check on the development Mac reached a saved 10% threshold, switched accounts, and verified the new shared login identity. Existing-chat adoption was not checked in that run.
- Disposable macOS Keychain and isolated Ubuntu GNOME Keyring roundtrips verified values of 524, 9,380, and 20,000 characters, including update and deletion. The Linux container did not mount user profiles.
- Feature commits excluded profiles, credentials, private state, and `.env` files. Secret scans and GitHub security checks passed. This does not replace the desktop checks below.

### Remaining desktop checks

| Check | Where it remains unverified |
| --- | --- |
| Existing chats adopt the new login and can continue | Two-terminal test on every target platform |
| Real Claude sign-in and automatic switching | Linux, Windows, and older/Intel Mac hardware |
| Monitor and credential store work after signing out and in | Each target desktop OS |
| Actual hardware/version support | macOS 13, Windows ARM64, other Linux distributions, and WSL |

Passing tests with a fake Claude screen or harmless background process does not verify these behaviors. macOS 12 and earlier are outside current Claude Code's supported OS range.

## 1. Check the dashboard

- The top bar should say **Connected locally**.
- The route should identify the current account and show which profiles need sign-in.
- Automatic switching should show **On**, a session percentage, and your saved threshold.
- **Switch at a reported limit too** should be checked unless you turned it off.

Compare the session percentage with `/usage` in Claude. Give the monitor about a minute to refresh; it checks the rendered usage display periodically.

## 2. Test two open Claude chats

This test changes the shared account used by your local Claude terminals. Choose a time when your chats are waiting for input and remember the starting account so you can switch back.

1. Open two Terminal windows in two projects and run `claude` in each. Both must use the default Claude login, without a separate profile or fixed token.
2. In each chat, send: **Reply with “ready” without editing files or running tools.**
3. In Switchboard, select another prepared account under **Switch manually** and click **Switch account**.
4. Wait for the current account and route marker to update. Record whether the OS credential store requests approval or unlocking.
5. In both existing chats, send: **Reply with “test complete” without editing files or running tools.** Keep the same chats open.
6. Check `/status` and `/usage` in both chats. They should reflect the selected account and allow you to continue. A changed dashboard alone proves the shared login changed; it does not prove an already-running chat adopted it.
7. Switch back to the starting account and repeat a short message in each chat.

**Pass:** both chats continue under the selected account, the dashboard records the switches, and routine switches do not produce repeated helper password prompts.

**If a chat shows the old account or stops:** note which terminal and action failed. Do not treat the dashboard change as proof that the chat changed accounts.

## 3. Test the usage threshold

Open **Name and settings** and confirm that an account's percentage can be saved and survives a refresh. Restore your preferred value after testing.

During normal work, watch the session usage approach that account's threshold. When a monitor check observes usage at or above it, the monitor should switch to the next available account and record **Session threshold** in recent switches. Allow about a minute for the next reading and switch.

To test without spending extra quota, if current usage is already above 0%, temporarily set the current account's threshold below that observed value. Keep the next account at its normal threshold. Restore the original setting immediately after the switch. Do not consume quota just to force a test.

The automated tests also simulate a 96% reading against a 95% threshold and verify rotation. That verifies the switching logic, while the live check verifies Claude's real usage display and login behavior.

## 4. Test fallback and background operation

- Turn **Switch at a reported limit too** off and back on. Refresh the page after each change and confirm the setting was saved.
- With fallback on, a real Claude rate-limit error should switch accounts when one is available. Retry the interrupted message after the switch.
- Close the dashboard launcher while switching is on. Before reopening it, run `cswitch status` (or `uv run shared_mode.py status` from the project folder) twice, at least a minute apart. `session_usage.checked_at` should advance and its account should match the current login. Record any usage error. Then reopen Switchboard. Checking before reopening matters because the launcher can restart an enabled monitor.

Automated tests simulate the rate-limit event and verify that disabling fallback prevents that event from switching accounts. No real quota needs to be exhausted for this check.

### Check startup after signing in again

1. Save your work and leave automatic switching on. Record the current account and usage reading.
2. Sign out of the OS and sign back in as the same user. On Linux/macOS, unlock the credential store if asked.
3. Before opening Switchboard, run `cswitch status` (or `uv run shared_mode.py status` in its project folder) twice, about a minute apart. Allow up to two minutes for the first usage check.
4. Confirm `session_usage.checked_at` advances without launching the dashboard and note any error.
5. Open Switchboard and confirm its state matches. Repeat a short Claude message and the two-terminal check if needed.

**Pass:** the monitor reads usage after sign-in without being started by the dashboard, the credential store is usable, and real Claude requests succeed. Record OS version, processor, and any approval prompts; do not describe this check as passed based only on a task or service being registered.

## 5. Check account controls

Rename an account, refresh, and confirm the saved name appears in the route and account list. Open its **i** button and check the details and activity.

For a deletion test, add an unused profile and delete that profile. Confirm it disappears from Switchboard. Test logout only on an account you are willing to sign out of. The active shared account cannot be removed while switching is on.

## Report a failure

Include the action, the expected result, the actual result, your operating system, and the Claude Code version from `claude --version`. Share the visible error text, with account emails and organization IDs removed. Do not share credentials, profile files, private dashboard links, or raw authentication logs.

## Run the automated tests

```sh
uv sync --group tests
uv run --group tests python -B -m unittest discover -s tests -v
node --check ui/app.js
```

The suite uses fake Claude responses and disposable credentials, not your accounts. Native Windows checks write and delete test Credential Manager entries; on native Windows, they also run a harmless scheduled task and test process cleanup.

On macOS, the LaunchAgent test is opt-in outside CI. It creates and removes a unique disposable agent:

```sh
SWITCHBOARD_TEST_LAUNCHD=1 uv run --group tests python -B -m unittest discover -s tests -p test_macos_agent.py -v
```

On Linux, run the service test only inside a working systemd user session. It creates and removes a unique disposable service:

```sh
SWITCHBOARD_TEST_SYSTEMD=1 uv run --group tests python -B -m unittest discover -s tests -p test_linux_service.py -v
```

Both tests are included in merged `main`. Do not opt in from a machine without the matching OS session. Normal test runs skip them unless enabled. CI enables the Mac probe on Mac runners and uses a separate Ubuntu 22.04 service job for Linux.

CI runs Ubuntu, macOS, and Windows on Python 3.9 and 3.14, plus macOS 14 / Python 3.9, macOS 15 Intel / Python 3.14, and a separate Ubuntu 22.04 systemd service job. Real GNOME Keyring was checked separately in an isolated Ubuntu container. Repeat the desktop checklist on each actual machine; successful component tests do not establish live account switching or login startup.
