# Herdr Bartender Plugin

A lightweight, zero-dependency [Herdr](https://github.com/voodootikigod/herdr) plugin that mirrors AI coding agent activity into **[Bartender 6](https://www.macbartender.com/) Pro's Top Shelf** on macOS.

Top Shelf displays real-time agent status via its NotchBar AI Agent HTTP bridge, giving you glanceable visibility into your active agents right in your menu bar or notch.

---

## Features

- **Real-Time Status Sync**: Automatically maps Herdr agent states (`working`, `blocked`, `done`, `idle`, `closed`) to Bartender Top Shelf states (`Working`, `Waiting`, `Done`, `Idle`, `Ended`).
- **Multi-Agent Support**: Out-of-the-box recognition for Antigravity (`agy`), Claude Code, OpenAI Codex, Gemini CLI, Cursor, OpenCode, GitHub Copilot, and more.
- **Antigravity (`agy`) CLI Integration**: Native support both inside Herdr panes and standalone across Ghostty, iTerm2, Terminal.app, Warp, and VS Code.
- **Vendor Hook Deduplication**: Patches Bartender's built-in Claude Code and Codex hooks so agents running inside Herdr never appear twice in your menu bar.
- **Fail-Open & Resilient**: Background reconciler handles retries (exponential backoff), session TTL expiry, orphan recovery, and crash cleanup. If Herdr stops, vendor hooks fall through cleanly.
- **Zero External Dependencies**: Pure Python 3 (3.9+, standard library only) plus POSIX shell scripts. No third-party packages or package managers required.

---

## How It Works

### Status Mapping

| Herdr Event / Agent Status | Top Shelf State | Menu Bar Appearance | Description |
| :--- | :--- | :--- | :--- |
| `pane.agent_status_changed`, `working` | `Working` | Animated spinner | Agent is executing tools or generating code |
| `pane.agent_status_changed`, `blocked` | `Waiting` | Attention highlight | Agent needs user input, confirmation, or review |
| `pane.agent_status_changed`, `done` | `Done` | Completion icon | Task successfully completed |
| `pane.agent_status_changed`, `idle` | `Idle` | Resting icon | Agent session open and awaiting commands |
| `pane.agent_status_changed` (agent exited) | `Ended` | Dismissed | Agent process finished |
| `pane.closed`, `tab.closed`, `workspace.closed` | `Ended` | Dismissed | Pane, tab, or workspace closed |

### Agent Identity & Display

- **Herdr Panes**: Agents display as `<Agent> (Herdr)` (e.g., `Antigravity (Herdr)`, `Claude (Herdr)`, `Codex (Herdr)`, `Gemini (Herdr)`, `Cursor (Herdr)`, `OpenCode (Herdr)`, `GitHub Copilot (Herdr)`).
- **Session Keys**: Each Herdr session is uniquely tracked as `herdr:<host>:<workspace>:<pane>`. Shell panes without an active agent are ignored, and transient `unknown` statuses never evict an active session.
- **Safe Delivery**: All updates pass through a locked, sequenced sender with retries at 0s, 1s, 2s, 4s, and 8s.
- **Automatic Reconciliation**: A background reconciler manages session TTLs (Working: 12h, Idle/Done: 24h, Waiting: 48h), restarts, and unconfirmed orphan recovery.

---

## Requirements

- **macOS** (Apple Silicon or Intel)
- **Bartender 6 (Pro)** with Top Shelf enabled (default bridge port: `127.0.0.1:7823`)
- **Herdr >= 0.9.0**
- **Python 3.9+** (pre-installed on macOS or via Homebrew)

> [!NOTE]
> The plugin connects strictly to `http://127.0.0.1:<port>` over loopback, ignores system proxy variables, never follows redirects, and only communicates when a Bartender process is actively running.

---

## Quick Start / Installation

### 1. Install via Herdr Plugin Manager
Install directly from GitHub via the Herdr CLI:

```bash
herdr plugin install voodootikigod/herdr-bartender
```

> [!TIP]
> **Developing locally?** You can clone the repository and link it into Herdr instead:
> ```bash
> git clone https://github.com/voodootikigod/herdr-bartender.git ~/Projects/herdr-bartender
> herdr plugin link ~/Projects/herdr-bartender
> ```

### 2. Verify plugin registration
Restart Herdr, then verify that the plugin is recognized:

```bash
herdr plugin list
```

You should see `herdr-bartender` in the output.

[`herdr-plugin.toml`](herdr-plugin.toml) registers:
- A startup hook (`./bin/herdr-bartender --reconcile-background`) which spawns the background reconciler and exits.
- Event handlers for `pane.agent_status_changed`, `pane.closed`, `tab.closed`, and `workspace.closed`. Each invocation is bounded by a 1.5s watchdog (well within Herdr's 2.0s timeout).

### 3. Verify connectivity
Verify that Bartender Top Shelf is running and reachable:

```bash
./bin/herdr-bartender --health
```

To run an end-to-end simulation that creates, cycles through states, and dismisses a test item on Top Shelf:

```bash
./bin/herdr-bartender --live-test
```

### 4. (Recommended) Install Vendor Hook Deduplication
If you use Claude Code or Codex, install the deduplication guard so agents don't appear twice:

```bash
./bin/herdr-bartender --install-hooks
```

---

## Vendor Hook Deduplication Guard

Bartender ships built-in hooks for Claude Code (`claude-event-hook.sh`) and Codex (`codex-notify-hook.sh`). Without deduplication, an agent running inside a Herdr pane would be reported twice: once by Herdr and once by Bartender's native hook.

### How it works
The guard patches Bartender's vendor hooks non-destructively:
- **Herdr Active**: While Herdr actively manages a pane, the guard intercepts the vendor hook and lets Herdr control Top Shelf.
- **Fail-Open Fallback**: If Herdr is closed, a delivery fails, or a pane marker expires (after 60 seconds), the guard immediately falls through to the native vendor hook.

### Managing Hook Patches

```bash
# Install the guard into vendor hooks and record clean SHA-256 signatures:
./bin/herdr-bartender --install-hooks

# Remove the guard cleanly (restores byte-for-byte original hooks):
./bin/herdr-bartender --uninstall-hooks

# Check hook status and detect upstream modifications:
./bin/herdr-bartender --health
./bin/herdr-bartender --status
```

**`--install-hooks`**
- Patches each hook atomically: writes a temporary file, validates syntax with `bash -n`, ensures no concurrent modification, and replaces the hook atomically while preserving executable permissions.
- Keeps a `.pristine` backup of each original hook.
- Records the clean SHA-256 of each hook in `vendor-hook-sha.json`.
- Clears warning flags (`NO_HOOKS`, `HOOK_NEEDS_REVIEW`).

**`--uninstall-hooks`**
- Writes `NO_HOOKS` to disable future patching.
- Removes exactly the injected bytes, restoring the original file and permissions byte-for-byte.

### Upstream Hook Updates (`HOOK_NEEDS_REVIEW`)
If Bartender updates its vendor hooks in a new release:
1. The background reconciler detects the checksum change and leaves the hook untouched.
2. `--status` displays a warning:
   ```text
   [WARNING] Vendor hook modified upstream (SHA mismatch). Run 'herdr-bartender --install-hooks' to re-verify and approve changes.
   ```
3. Run `./bin/herdr-bartender --install-hooks` to re-verify, patch, and approve the updated hooks.

---

## Antigravity (`agy`) CLI Support

Google Antigravity CLI sessions are fully supported both inside Herdr and standalone across Ghostty, iTerm2, Terminal.app, Warp, and VS Code:

- **Inside Herdr**: Herdr natively detects `agy` and emits `pane.agent_status_changed`. The plugin translates this to `Antigravity (Herdr)` on Top Shelf.
- **Standalone `agy`**: Sessions report directly to Bartender Top Shelf via `scripts/agy-notify-hook.sh`.
- **Automatic Deduplication & Handoff**: If an `agy` session runs inside Herdr (`HERDR_PANE_ID` is set):
  - While Herdr is healthy, direct notifications are suppressed so `herdr-bartender` manages Top Shelf updates with zero duplicate entries.
  - If Herdr is temporarily down or recovering, the hook fails open, records `.vendor_active`, and preserves `Stop` to ensure direct entries are cleanly dismissed when the session exits.

### Standalone Hook Configuration (`~/.gemini/config/hooks.json`)

To enable standalone `agy` reporting, copy `scripts/agy-notify-hook.sh` to Bartender's hooks directory (or keep it in the repo):

```bash
mkdir -p "$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks"
cp scripts/agy-notify-hook.sh "$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh"
chmod +x "$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh"
```

Then add the following configuration to `~/.gemini/config/hooks.json`:

```json
{
  "bartender-topshelf": {
    "PreInvocation": [
      {
        "type": "command",
        "command": "if [ -x \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\" ]; then AGY_HOOK_EVENT='PreInvocation' \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\"; else printf '{}\\n'; fi",
        "timeout": 5
      }
    ],
    "PostInvocation": [
      {
        "type": "command",
        "command": "if [ -x \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\" ]; then AGY_HOOK_EVENT='PostInvocation' \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\"; else printf '{}\\n'; fi",
        "timeout": 5
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "if [ -x \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\" ]; then AGY_HOOK_EVENT='Stop' \"$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh\"; else printf '{\"decision\":\"\"}\\n'; fi",
        "timeout": 5
      }
    ]
  }
}
```

> [!NOTE]
> **Antigravity Hook Protocol**:
> Status-bar reporting is driven strictly by lifecycle status events: `PreInvocation` (`Working`), `PostInvocation` (`Idle`), and `Stop`/`SessionEnd` (`Ended`), keeping tool execution completely unaffected with zero overhead. `PreToolUse` is not registered as a status hook since it functions as an authorization gate in Antigravity; any non-lifecycle event exits 0 immediately without side-effects or delay.

---

## CLI Reference

All flags are handled in [`herdr_bartender/cli.py`](herdr_bartender/cli.py). Running without arguments or with `--help` prints usage.

| Command | Purpose | Exit Code |
| :--- | :--- | :--- |
| `herdr-bartender <event>` | Handles Herdr plugin event envelope from `stdin` (bounded to 1.5s). | `0` |
| `--health` | Prints bridge `/health` JSON plus `hooks_guard_intact`. | `0` (or `{"error": ...}`) |
| `--status` | Prints active session count, session states, delivery status, and warnings. | `0` on success, `1` on error |
| `--sessions` | Dumps the active session cache as JSON. | `0` on success, `1` on error |
| `--install-hooks` | Patches vendor hooks with deduplication guard. | `0` on success, `1` on error |
| `--uninstall-hooks` | Restores original unpatched vendor hooks. | `0` on success, `1` on error |
| `--cleanup` | Ends every tracked session on Top Shelf. | `0` (success), `2` (partial), `1` (fatal) |
| `--replay-orphans <file>` | Replays an orphan export file after Bartender restart. | `0` on completion, `1` on error |
| `--live-test` (alias `--test`) | Runs an end-to-end integration check against the live Bartender bridge. | `0` (PASS), `1` (FAIL) |
| `--unit-test` | Runs the full unit test suite with hang watchdog. | `0` (PASS), `1` (FAIL) |
| `--reconcile-background` | Startup hook: spawns the detached background reconciler and exits. | `0` on spawn, `1` on failure |

### `--cleanup` and Orphan Recovery

`--cleanup` stages `Ended` for every cached session and delivers each through the normal sender with a 0.15s socket timeout:
- **Exit 0:** Every session was confirmed Ended (HTTP 200).
- **Exit 2:** At least one session was not confirmed (bridge unreachable or rejected). Unconfirmed sessions are exported to `~/.herdr-bartender-orphans.json` (mode `0600`, outside the state directory).
- **Exit 1:** Fatal error.

Once Bartender is running again, replay the unconfirmed sessions:

```bash
./bin/herdr-bartender --replay-orphans ~/.herdr-bartender-orphans.json
```

The background reconciler also automatically replays the orphan file whenever `/health` succeeds, with an exponential backoff between attempts.

---

## Configuration & State

### Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `NOTCHBAR_AGENTS_PORT` | `7823` | Port for the Bartender NotchBar HTTP bridge (`1024-65535`). If invalid or unset, defaults to `7823` and logs a warning. Set in Herdr's environment if non-default. |
| `HERDR_PLUGIN_STATE_DIR` | `~/.local/state/herdr/plugins/herdr-bartender` | Overrides the plugin's runtime state directory. |
| `HERDR_BARTENDER_VENDOR_HOOKS_DIR` | `~/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks` | Overrides Bartender's vendor hooks directory. |

### State Directory

The state directory is resolved identically by Python, the hook guard, and [`scripts/rollback.sh`](scripts/rollback.sh):

```bash
${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}
```

The directory is created with `0700` permissions and files with `0600`:

```text
herdr-bartender/
├── active-sessions.json      # Session cache
├── plugin.log                # Diagnostics log (rotated at 1MB)
├── vendor-hook-sha.json      # Approved SHA-256 hashes of vendor hooks
├── panes/                    # Pane markers (.failed, .vendor_active)
├── spool/ & results/         # Spooled events and deferred reconciler work
└── DISABLED                  # Flag file: when present, all plugin events are no-ops
```

> [!TIP]
> While `DISABLED` exists, all events and the background reconciler do nothing. `--cleanup` and CLI commands still operate. Delete the file to re-enable the plugin.

---

## Upgrading

To update the plugin to the latest version:

```bash
cd ~/Projects/herdr-bartender
git pull
```

Because the background reconciler keeps running in the background and holds `reconciler.lock`, stop it after pulling so the new code is loaded on the next event:

```bash
pkill -u "$(id -u)" -f '^[^ ]*[Pp]ython[^ /]* .*/herdr-bartender --reconcile-background( --[a-z-]+)*$'
```

The next Herdr event will automatically start a reconciler with the updated code. Run `./bin/herdr-bartender --status` and `./bin/herdr-bartender --health` to confirm the update.

---

## Uninstallation / Rollback

To cleanly uninstall the plugin, remove all hooks, and reset Top Shelf state, run the automated rollback script:

```bash
scripts/rollback.sh
```

The rollback script runs these steps in order:
1. Creates the `DISABLED` flag to halt incoming events.
2. Removes plugin symlinks (`~/.config/herdr/plugins/herdr-bartender`).
3. Waits for any active event handlers to exit and terminates running reconcilers.
4. Runs `--cleanup` to dismiss all active sessions on Top Shelf.
5. Runs `--uninstall-hooks` to cleanly remove the vendor deduplication guard.
6. Safely deletes the plugin state directory.

---

## Development & Testing

### Running Tests

```bash
# Verbose run with hang watchdog:
./bin/herdr-bartender --unit-test

# Or run via standard unittest:
python3 -m unittest discover -s tests -t .
```

The test suite uses standard library `unittest` and runs on macOS and Linux. Every test runs in an isolated sandbox (`tests/support`):
- Temporary `HOME`, `XDG_STATE_HOME`, state directory, and vendor hooks directory.
- PATH shims for `pgrep`, `ps`, `osascript`, and `herdr`.
- Scriptable mock bridge on an ephemeral port.
- Injectable fake clock and recording process spawner.

No test touches your real home directory, live Bartender process, or installed hooks.

### Live Acceptance Checklist

To test against live software on macOS:

1. **Bridge Verification (Bartender 6 with Top Shelf enabled):**
   - Run `./bin/herdr-bartender --live-test`.
   - Watch the test entry appear in Top Shelf, cycle through `Working`, `Waiting`, `Done`, `Idle`, and dismiss (`Ended`).
   - Confirms `RESULT: PASS (7/7 checks)`.
2. **Herdr Integration:**
   - Link the plugin into Herdr and launch an agent (e.g. Claude Code or Antigravity) in a pane.
   - Verify that status changes reflect in Top Shelf and closing the pane dismisses the entry.
3. **Vendor Handover:**
   - With vendor hooks installed (`--install-hooks`), start `claude` in a Herdr pane, then stop Herdr.
   - Verify that the vendor hook falls through and Top Shelf shows the native entry.
   - Restart Herdr and verify that Herdr reclaims the pane and clears any duplicate vendor entry.

### Architectural Specifications

For comprehensive architectural design records, see:
- [`herdr-bartender-plan.md`](herdr-bartender-plan.md) — Comprehensive technical architecture specification.
- [`docs/plan-resolutions.md`](docs/plan-resolutions.md) — Architectural resolutions and invariant catalog.
- [`docs/traceability.md`](docs/traceability.md) — Invariant-to-test traceability matrix.

---

## License

[MIT](LICENSE) © 2026 Chris Williams

