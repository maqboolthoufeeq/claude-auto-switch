# Platform support and next steps

This table distinguishes the current implementation from planned platform work. Passing simulated tests does not verify a real operating system's credential store, background service, or Claude login.

| Platform | Current status | Work needed before full support |
| --- | --- | --- |
| Current Apple Silicon macOS | Dashboard, live session reader, and background monitor checked locally; real two-terminal switch test still needed | Complete the live test in [testing.md](testing.md) |
| Older macOS 13+ and Intel Macs | Uses the same macOS implementation; older systems and Intel hardware have not been tested | Test installation, Keychain access, launch agent, usage reader, and live switching on these systems |
| Linux and Ubuntu | The guided task runner is designed for Unix; shared switching still requires macOS | Add a secure Secret Service credential backend, a user background service, Linux launch instructions, and real Claude tests |
| Native Windows | Not supported yet; Unix-only imports currently prevent startup | Replace file locking and process control, add a Windows terminal adapter, secure credential storage, background task, and launcher |
| Windows with WSL | Depends on the Linux implementation; shared switching is not ready | Test the Linux backend inside WSL; keep Claude and Switchboard in the same WSL environment |

## Target versions

The initial targets follow [Claude Code's published system requirements](https://code.claude.com/docs/en/setup#system-requirements): macOS 13+, Windows 10 version 1809+, Ubuntu 20.04+, and Debian 10+. Python 3.9+ is also required by Switchboard and can be managed by uv.

Support for macOS older than 13 would require a compatible Claude Code version as well as Switchboard changes. Do not assume that changing this app can make the current Claude binary run on an older operating system.

## Extension order

1. Complete the current macOS live switching test and fix any failures.
2. Add Linux/Ubuntu credential storage and background monitoring. Keep credentials out of plaintext Switchboard files and process arguments.
3. Add native Windows credential protection, terminal handling, locking, process control, and background monitoring.
4. Verify older macOS and Intel Macs without requiring the old Swift migration helper for a new installation.
5. Publish platform instructions and mark a platform supported only after its real credential and two-terminal tests pass.

Every platform must keep the dashboard on port **56729**, preserve account names and thresholds, use current session usage, provide the consent-controlled limit fallback, and protect saved credentials.
