# Test Switchboard

Use the checks below on each operating system. Start through its launcher and keep the dashboard on port **56729**. Follow [platform setup](platform-support.md) first.

## Verified on October 13, 2026

- A real background check read the current session percentage from Claude's rendered `/usage` screen. At 10% usage and a saved 10% threshold, it switched from account-5 to account-1. Claude's shared account identity was verified after the switch.
- A rejected account-6 login was skipped. That account needs a fresh sign-in and preparation before it can rejoin rotation.
- Disposable Keychain values of 524, 9,380, and 20,000 characters were written, read back exactly, updated, and removed. All six prepared credentials were checked for complete, readable JSON; this does not prove all six refresh tokens are still accepted by Claude.
- Before the platform extension, all 32 automated tests passed on macOS with Python 3.9 and 3.14, and in isolated Ubuntu 22.04 with Python 3.10. Those Ubuntu checks simulated credentials and Claude.

The two-terminal chat continuation check below, native Windows, and older macOS hardware remain unverified.

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

During normal work, watch the session usage approach that account's threshold. When the observed percentage reaches it, the monitor should switch to the next available account and record **Session threshold** in the recent switches. Do not consume quota just to force this test.

The automated tests also simulate a 96% reading against a 95% threshold and verify rotation. That verifies the switching logic, while the live check verifies Claude's real usage display and login behavior.

## 4. Test fallback and background operation

- Turn **Switch at a reported limit too** off and back on. Refresh the page after each change and confirm the setting was saved.
- With fallback on, a real Claude rate-limit error should switch accounts when one is available. Retry the interrupted message after the switch.
- Close the launcher, keep using your Claude terminals, then reopen Switchboard. The background monitor should still be active and report a recent session reading.

Automated tests simulate the rate-limit event and verify that disabling fallback prevents that event from switching accounts. No real quota needs to be exhausted for this check.

## 5. Check account controls

Rename an account, refresh, and confirm the saved name appears in the route and account list. Open its **i** button and check the details and activity.

For a deletion test, add an unused profile and delete that profile. Confirm it disappears from Switchboard. Test logout only on an account you are willing to sign out of. The active shared account cannot be removed while switching is on.

## Report a failure

Include the action, the expected result, the actual result, your operating system, and the Claude Code version from `claude --version`. Share the visible error text, with account emails and organization IDs removed. Do not share credentials, profile files, private dashboard links, or raw authentication logs.

## Run the automated tests

```sh
uv run python -B -m unittest discover -s tests -v
node --check ui/app.js
```

These tests simulate Claude and saved credentials. They do not switch your real accounts.

The extended suite also exercises real terminal redraws and file-lock contention. On native Windows it writes, reads, and deletes disposable Credential Manager entries. GitHub Actions runs it on Ubuntu, macOS, and Windows with Python 3.9 and 3.14. Linux's real GNOME Keyring is checked separately in an isolated Ubuntu container, without mounting account profiles.

For Linux, also close the launcher and verify the systemd user service continues updating usage. For Windows, repeat after signing out and back in to verify Task Scheduler startup. For older supported macOS, repeat with that OS version and processor; tests on the current Mac do not establish older-version compatibility.
