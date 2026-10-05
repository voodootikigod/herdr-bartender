# Herdr Bartender Plugin

A Herdr plugin that mirrors Herdr agent status into **Bartender Pro's Top Shelf** on macOS. Top Shelf exposes this status through its NotchBar AI Agent HTTP bridge.

The spec is [`herdr-bartender-plan.md`](herdr-bartender-plan.md). [`docs/plan-resolutions.md`](docs/plan-resolutions.md) amends it, and where the two disagree, the resolutions file wins.

## What it does

| Herdr event | Top Shelf state |
| :--- | :--- |
| `pane.agent_status_changed`, `agent_status: working` | `Working` (spinner) |
| `pane.agent_status_changed`, `agent_status: blocked` | `Waiting` (attention highlight) |
| `pane.agent_status_changed`, `agent_status: done` | `Done` |
| `pane.agent_status_changed`, `agent_status: idle` | `Idle` |
| `pane.agent_status_changed` with `agent` null/empty (agent exited) | `Ended` |
| `pane.closed`, `tab.closed`, `workspace.closed` | `Ended` (entry removed) |

- Agents show as `<Agent> (Herdr)`, for example `Claude (Herdr)` or `Codex (Herdr)`.
- Each session is identified as `herdr:<host>:<workspace>:<pane>`.
- Shell panes with no agent are ignored, and an `unknown` status never evicts a session.
- Every delivery goes through one locked, sequenced sender. A failed delivery is retried at 0/1/2/4/8s.
- A background reconciler owns retries, TTLs (Working 12h, Idle/Done 24h, Waiting 48h), restart re-syncs and orphan replay.
- An optional guard patched into Bartender's own Claude/Codex hook scripts stops the same agent from showing twice. The guard falls back to the vendor hooks whenever Herdr is unhealthy.
- The plugin is pure Python 3 (3.9+, stdlib only) plus one bash guard. It has no dependencies.

## Scope and requirements

- **macOS**, with Bartender 6 (Pro) running and Top Shelf enabled. The bridge listens on `127.0.0.1:7823`.
- **Herdr 0.9.x**. The manifest declares `min_herdr_version = "0.9.0"`. The plan's "Herdr 0.8.x" text describes the event/plugin contract, which 0.9.x keeps (R29).
- **Single host, loopback only.** The plugin only ever connects to the literal `http://127.0.0.1:<port>`, ignores proxy variables, and never follows redirects. It also sends nothing unless a `Bartender 6`/`Bartender` process is running. Remote Herdr hosts, SSH reverse tunnels, Tailscale and `NOTCHBAR_AGENTS_HOST` are not supported in v1.0 (Plan §8, R22).

### Configuration

| Variable | Effect |
| :--- | :--- |
| `NOTCHBAR_AGENTS_PORT` | Bridge port. It must be an integer from 1024 to 65535. If it is unset or empty, the port is `7823`. Any other value (`abc`, `80`, `70000`, `7823x`) also falls back to `7823` and logs a `WARNING` to `plugin.log`. Set it in the environment Herdr runs plugins with. |
| `HERDR_PLUGIN_STATE_DIR` | Overrides the state directory (see below). |
| `HERDR_BARTENDER_VENDOR_HOOKS_DIR` | Overrides the vendor hooks directory. The default is `~/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks`. |

### State directory

The state directory is resolved identically by Python, the hook guard and `scripts/rollback.sh` (Plan §10.1 #66):

```
${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}
```

The directory is created `0700`, and its files `0600`. It holds:

- `active-sessions.json`: the session cache.
- `plugin.log`, rotated at 1MB.
- `panes/`: pane markers, `.failed` and `.vendor_active`.
- `spool/` and `results/`: deferred work.
- `vendor-hook-sha.json`
- Flag files: `DISABLED`, `DELIVERY_DOWN`, `NO_HOOKS`, `HOOK_NEEDS_REVIEW` and `reconciler.pending`.

While `DISABLED` exists, every event and the reconciler do nothing. `--cleanup` and the explicit CLI commands still run. Delete the file to re-enable the plugin.

## Installation

1. Put the repository somewhere permanent, for example `~/Projects/herdr-bartender`.
2. Link it into Herdr's plugin directory. The launcher resolves symlinks, so it runs from the repo.
   ```bash
   mkdir -p ~/.config/herdr/plugins
   ln -s ~/Projects/herdr-bartender ~/.config/herdr/plugins/herdr-bartender
   ```
3. Restart Herdr, then check that `herdr plugin list` shows `herdr-bartender`.

   [`herdr-plugin.toml`](herdr-plugin.toml) registers:
   - a startup hook, `./bin/herdr-bartender --reconcile-background`, which starts the detached reconciler and exits;
   - the four events above, each as `./bin/herdr-bartender <event-name>`.

   Herdr streams the `{event, data, context}` JSON envelope on stdin. Each event invocation is bounded by a 1.5s watchdog, inside Herdr's 2.0s limit.
4. Optional: install the dedup guard (next section).
5. Check the setup with `./bin/herdr-bartender --health`. To exercise Top Shelf end to end, run `./bin/herdr-bartender --live-test`.

### Upgrading in place

A `git pull` does not reach a reconciler that is already running: it keeps its old code in memory and keeps
`reconciler.lock` while any session is live, so the new reconciler cannot start (R38). After pulling, stop it:

```bash
pkill -u "$(id -u)" -f '^[^ ]*[Pp]ython[^ /]* .*/herdr-bartender --reconcile-background( --[a-z-]+)*$'
```

The next Herdr event starts a reconciler with the new code. `--status` and the startup hook warn while an older
reconciler (one without the current `reconciler.stamp`) still holds the lock. The stamp's version is a digest of the
package sources, so this also fires after any later `git pull` that changed the code (R43). Hooks patched by the pre-package
monolith (a blank line before the guard) are recognised: the reconciler re-lays them out with the current guard
without asking for a review, and uninstall restores the original bytes (R37).

## Vendor hook dedup guard

Bartender ships its own hooks for Claude Code (`claude-event-hook.sh`) and Codex (`codex-notify-hook.sh`). Without the guard, an agent running in a Herdr pane shows up twice: once from Herdr and once from the vendor hook.

The guard suppresses a vendor notification only while Herdr demonstrably owns that pane. In every other case it falls through to the vendor hook:
- Herdr is dead.
- The pane marker is stale (more than 60s old) or missing.
- The pane has a `.failed` delivery.
- `DISABLED` or `DELIVERY_DOWN` is set.
- The event is session-terminal.
- The event cannot be classified (awk missing, failing, or not done within its 1s deadline; R41).

```bash
./bin/herdr-bartender --install-hooks     # patch both hooks; records their clean SHA-256 (approval)
./bin/herdr-bartender --uninstall-hooks   # strip the guard byte-exactly; writes NO_HOOKS
./bin/herdr-bartender --status            # sessions + HOOK_NEEDS_REVIEW warning
./bin/herdr-bartender --health            # bridge /health + hooks_guard_intact
```

**`--install-hooks`**
- Patches each hook atomically: it writes a temp file, checks it with `bash -n`, checks the hook was not changed concurrently, then `os.replace`s it. The mode is kept, with `orig_mode | 0o100`.
- Keeps a `.pristine` backup of each hook.
- Records the clean SHA-256 of each hook in `vendor-hook-sha.json`.
- Clears `NO_HOOKS`, `HOOK_NEEDS_REVIEW` and `.hook_review_alerted`.
- Exits 0 only when every hook it found carries the current guard. It exits 1 if a hook could not be patched, or if no vendor hooks were found.

**`--uninstall-hooks`**
- Writes `NO_HOOKS` first. This makes the uninstall sticky: the reconciler never re-patches, and queued vendor dismissals are cancelled.
- Removes exactly the bytes the installer inserted, keeping the original mode.
- Run `--install-hooks` to undo it.

**Hooks changed upstream (`HOOK_NEEDS_REVIEW`)**
- The reconciler re-patches a hook automatically only when its guard-free content still matches its recorded SHA.
- On any mismatch, it patches nothing and writes the per-hook causes to `HOOK_NEEDS_REVIEW`. It also shows a single macOS notification, gated on `.hook_review_alerted`. The log warning is written once per change of causes, not on every pass.
- `--status` then prints `[WARNING] Vendor hook modified upstream (SHA mismatch). Run 'herdr-bartender --install-hooks' to re-verify and approve changes.`, followed by a `Review needed:` line naming each hook's cause.
- If you never ran `--install-hooks` (no `vendor-hook-sha.json` and no guard in any hook), the guard is simply not installed (R42): nothing is flagged, alerted or logged.
- Until you approve the change by re-running `--install-hooks`, the vendor hooks run unguarded, so you may see duplicate entries.

## CLI reference

All flags are handled in [`herdr_bartender/cli.py`](herdr_bartender/cli.py). A first argument that is not an option is treated as an event invocation. `--help` (or `-h`) prints usage; any other unknown option (a typo such as `--install-hook`) prints usage to stderr and exits 2 without touching anything (R51).

| Command | Purpose | Exit code |
| :--- | :--- | :--- |
| `herdr-bartender <event>` (stdin envelope) | Herdr event path, bounded to 1.5s. The event name comes from the envelope, then from argv, then from the legacy `HERDR_PLUGIN_EVENT*` variables (R20). | 0 |
| `--reconcile-background` | Startup hook: spawns the singleton reconciler detached (`--foreground` is the internal child flag). | 0, or 1 if the spawn failed |
| `--health` | Prints bridge `/health` JSON plus `hooks_guard_intact` (false for a missing, stale or legacy-layout guard). On failure it prints `{"error": <reason>}`: `bartender_not_running` (no Bartender process found, nothing sent), `invalid_bridge_url`, or `unreachable` (the request failed, or the process probe failed) (R51). | 0 |
| `--status` | Prints the active session count and each session's state and delivery status, plus the `HOOK_NEEDS_REVIEW` and outdated-reconciler (R38) warnings. | 0, or 1 if the cache is unavailable |
| `--sessions` | Dumps the session cache as JSON. | 0, or 1 if the cache is unavailable |
| `--install-hooks` / `--uninstall-hooks` | See above. | 0 on success, 1 otherwise |
| `--cleanup` | Ends every tracked session on Top Shelf (see below). | 0, 2 or 1 |
| `--replay-orphans <file>` | Replays an orphan export (see below). | 0 when every record is cleared, otherwise 1 |
| `--live-test` (alias `--test`) | Live acceptance check against the real bridge (see below). | 0 PASS, 1 FAIL |
| `--unit-test` | Runs the unittest suite. | 0 pass, 1 fail, 2 if `tests/` is missing |

### `--cleanup` and orphans

`--cleanup` stages `Ended` for every cached session, salvaged ones included, and delivers each through the normal sender with a 0.15s socket timeout. The whole run gets a budget of `max(10s, 0.15s × sessions)`. It ignores the 1.5s watchdog and runs even while `DISABLED` exists, because rollback sets that flag first.

- **Exit 0:** every session was confirmed Ended (HTTP 200).
- **Exit 2:** at least one session was not confirmed. The bridge was unreachable, rejected the session, ran out of retries, or a session was re-admitted while cleanup ran (R31). The unconfirmed Endeds are exported to `~/.herdr-bartender-orphans.json` (mode `0600`, outside the state dir).
- **Exit 1:** fatal error.

Once Bartender is running again, replay the export:

```bash
./bin/herdr-bartender --replay-orphans ~/.herdr-bartender-orphans.json
```

Replay works on the file under its `.lock`, at most 256 records per run (R33).
- A record whose session is live again in the cache is dropped without being sent.
- Every other record is sent as `Ended`, retried once with the minimal payload if needed.
- Confirmed records are removed from the file, and unconfirmed ones are kept. The file is unlinked once it is empty.

The reconciler also replays the file automatically whenever `/health` is ok, with a per-record backoff of 20s doubling to 300s (R27). Replay never touches `DISABLED` or the state dir.

## Rollback

```bash
scripts/rollback.sh
```

It runs these steps, in order (Plan §9.1):
1. Touches `DISABLED`.
2. Removes the plugin symlinks, both `~/.config/herdr/plugins/herdr-bartender` and `.../plugins/local/herdr-bartender`. If `herdr` is on PATH, it runs `herdr plugin unlink` and checks that `herdr plugin list` no longer shows the plugin.
3. Waits up to 3s for event handlers that were already running when `DISABLED` appeared (each is bounded by the 1.5s event deadline; if one is still running, the state dir is kept), then kills this user's reconcilers.
4. Runs `--cleanup`.
5. Runs `--uninstall-hooks`. If the launcher is unusable, it falls back to a byte-exact strip.
6. Deletes the state dir, but only if every step succeeded, and only if it is a real directory (not a symlink) named `herdr-bartender`. A `HERDR_PLUGIN_STATE_DIR` override that names anything else, such as `/tmp` or a shared directory, is kept, and the script exits 1 so you can check it and remove it by hand (R53).

It exits 0 when everything was removed. Otherwise it exits 1, keeps `DISABLED`, and prints the `--replay-orphans` command if an orphan file exists. Set `HERDR_BARTENDER_BIN` to use a different launcher. The script honours `HERDR_PLUGIN_STATE_DIR`, `XDG_STATE_HOME` and `HERDR_BARTENDER_VENDOR_HOOKS_DIR`.

## Tests

```bash
./bin/herdr-bartender --unit-test                 # verbose run, with a hang watchdog
python3 -m unittest discover -s tests -t .        # same suite, plain unittest
```

The suite is stdlib `unittest` and runs on Linux and macOS. Every test runs in a sandbox from `tests/support`:
- a temporary `HOME`, `XDG_STATE_HOME`, state dir and vendor hooks dir;
- PATH shims for `pgrep`, `ps`, `osascript` and `herdr`;
- a scriptable mock bridge on an ephemeral port;
- an injectable fake clock;
- a recording spawner in place of the real reconciler spawn.

No test touches your real home directory, the real hooks, a real Herdr or Bartender process, or real `osascript`. Each §10.1 invariant has a test whose docstring starts `Plan §10.1 #N`.

## Live acceptance checklist (Plan §10.2)

Run these by hand on the Mac, against the real software:

1. **Bridge (Bartender 6 with Top Shelf enabled):**
   - Run `./bin/herdr-bartender --live-test`. It reads the `/health` session-count baseline, then POSTs `Working`, `Waiting`, `Done` and `Idle` 1s apart for a unique `herdr:<host>:hb-livetest:<id>` session, then `Ended` (R34).
   - Watch the entry appear in Top Shelf, change state, and disappear.
   - The command checks that every POST returned `ok:true` and that the `/health` count returns to the baseline. It prints `RESULT: PASS` or `RESULT: FAIL`.
   - Keep other agents quiet while it runs, since they move the count too.
2. **Herdr 0.9.x integration:**
   - With the plugin linked as above, start Herdr and run Claude Code in a pane.
   - Check that Working, Waiting, Done and Idle reflect in Top Shelf, and that closing the pane removes the entry.
3. **Vendor handover:**
   - With hooks installed, run `claude` in a Herdr pane, then `killall herdr`.
   - Check that the vendor hook falls through and Top Shelf shows the native (UUID) entry.
   - Restart Herdr. Check that `cleanup_vendor_active` dismisses the vendor entry and Herdr owns the pane again.

Diagnostics: `--status`, `--sessions`, `--health`, and `plugin.log` in the state dir.
