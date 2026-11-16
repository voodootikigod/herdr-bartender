# Herdr-Bartender Review, Enhancement Proposals & ADLC Implementation Spec

**Date**: 2026-11-16  
**Repository**: `/Users/voodootikigod/Projects/herdr-bartender`  
**Branch / HEAD**: `main` (`15c2fd7`)  
**Local Runtime**: macOS, Herdr `0.9.5` (`protocol: 22`), Bartender 6 (`127.0.0.1:7823`)  
**Connected Remote**: `dev-one.taildd69d8.ts.net` (id `67fb70193744bcafbfe715f8f26ec72d`, Linux, Herdr `0.9.0`)

---

## Executive Assessment

`herdr-bartender` demonstrates exceptionally strong core systems engineering for a zero-dependency stdlib Python plugin:
- **Lock-free I/O Universal Sender** (`step_a` -> `step_b` -> `step_c`) enforced at runtime via `CriticalSectionViolation` (`herdr_bartender/bridge.py:176-185`).
- **Bounded hot-path execution** (`1.5s` `SIGALRM` budget + `deferred_exit()`), preventing slow/hung network or `ps` probes from stalling Herdr event dispatch.
- **Crash-safe durability**: R10 orphan journaling, epoch-dominating cache salvage, R38 reconciler code-digest self-handoff, and the linear-time BWK `awk` JSON classifier in `herdr_bartender/hook_guard.sh`.

Cross-referencing the codebase against **live system state** uncovered critical integration gaps in both requested focus areas:
1. **Antigravity (`agy`) Integration**: Standalone `agy` Top Shelf reporting is **currently inert** on this machine (replaced on disk by a 51-byte emergency stub after a `PreToolUse` stdout contract mismatch), `agy` is excluded from `--install-hooks` and `--health`, standalone `Stop` events prematurely delete sessions after every turn, and inside Herdr a pane's `cwd`/`title` freeze permanently on its first working directory.
2. **Remote Herdr Connections (`dev-one.taildd69d8.ts.net`)**: Completely invisible today (**4 live remote agents**—3 `agy` working, 1 `claude` idle—are active in `herdr --machine dev-one.taildd69d8.ts.net api snapshot`, while `herdr-bartender --status` tracks `0`), blocked by Herdr's split client/server model, a local `pgrep` Bartender check, and single-host cache pinning.

---

## 1. Antigravity (`agy`) Integration: Findings & Approved Fixes

1. **`PreToolUse` Stdout Contract Hazard & CamelCase Event Parsing (`scripts/agy-notify-hook.sh`)**:
   - `scripts/agy-notify-hook.sh:8-20` previously exited `0` with empty stdout for unsupported events (breaking `PreToolUse`, which requires `{"decision":"allow"}`) and only grepped snake_case `"hook_event_name"` instead of Antigravity's camelCase `"hookEventName"`.
2. **Standalone `agy` Turn State Machine (`scripts/agy-notify-hook.sh:726-1085`)**:
   - Standalone `Stop` fires at the end of every prompt turn (`NO_TOOL_CALL`), not on CLI process exit, and can fire with `"fullyIdle": false` when background tasks run.
   - `PostInvocation` previously mapped to `"Idle"`, causing `Working -> Idle -> Working` flicker mid-turn.
   - `lastUserInput`, `fullyIdle`, `terminationReason`, and `error` on stdin were unused.
3. **First-Party `agy-notify-hook.sh` Deployment & Health Checks (`herdr_bartender/agy_hook.py`, `hooks_install.py`)**:
   - `agy-notify-hook.sh` is shipped by `herdr-bartender` itself (already containing Herdr pane suppression + `.vendor_active` handoff), so `--install-hooks`, `--uninstall-hooks`, `--health`, and `--status` should deploy and verify it without polluting `vendor-hook-sha.json` (which strictly tracks Bartender's vendor-owned Claude/Codex scripts).
4. **Inside-Herdr `agy` Metadata & Herdr `0.9.5` Events (`intake.py`, `staging.py`, `herdr-plugin.toml`)**:
   - Clear stale `cwd`/`title`/`tab_id` from `cached` when starting a new turn (`new_turn` in `staging.py`), honor `display_agent`, subscribe to `pane.moved` and `pane.exited`, and periodically refresh live pane metadata via `herdr api snapshot`.

---

## 2. Remote Herdr Connections (`dev-one.taildd69d8.ts.net`): Approved Architecture

1. **Approved Decision 1 — Option R1 Only (Local Multi-Endpoint Watcher on macOS)**:
   - Discover enabled SSH machines from `${XDG_STATE_HOME:-~/.local/state}/herdr/client/endpoints.json` (`ssh` entries with `"enabled": true`) and poll `herdr --machine <target_or_id> api snapshot` outside `active-sessions.lock`.
   - Debounce transient Tailscale/SSH disconnects via per-host `remote_unreachable_since[host]` with a `300.0`s grace window (mirroring `herdr_dead_since`), reconciling against a fresh snapshot upon reconnect.
2. **Approved Decision 2 — Remote Top Shelf Labeling**:
   - Format remote `agent` display strings as `<Agent> (Herdr · <remote_host>)` (e.g., `Antigravity (Herdr · dev-one)`), keeping local sessions as `<Agent> (Herdr)`, keeping `terminal: "Herdr"` unchanged (preserving Bartender app icon and click-to-focus behavior), and keeping `title` free of host prefixes.

---

## 3. Core Reliability & Housekeeping Fixes

1. **Quarantine & Stale Marker Sweep (`herdr_bartender/housekeeping.py`)**:
   - Call `salvage.prune_corrupt_quarantine(state_dir, now)` inside `sweep_stale_temp_files()` and sweep 0-byte `panes/*.vendor_active` touch files older than 7 days.
2. **`herdr server` Daemon Priority in `_probe_herdr()` (`herdr_bartender/process.py`)**:
   - Prefer `herdr server` over `herdr` TUI/CLI client processes when multiple non-GUI `herdr` candidates exist so opening/closing TUI clients never triggers false `herdr_restart` expirations.
3. **Test Shim Hot-Path Latency Fix (`tests/test_invariants_01_17.py`)**:
   - Use `#!/bin/sh` instead of `#!/usr/bin/env bash` in `DisabledFlagEventPathTests.setUp` log shims so macOS startup overhead stays well below `HOT_PATH_PROBE_TIMEOUT` (`0.25s`).

---

## 4. Verification Gates

- **Build Gate**: `python3 -m compileall -q herdr_bartender tests && bash -n scripts/agy-notify-hook.sh herdr_bartender/hook_guard.sh`
- **Test Gate**: `./bin/herdr-bartender --unit-test`

---

## 5. Executable Ticket DAG Specification (5 Disjoint Tickets)

Every ticket below owns a strictly disjoint `scope` (zero shared files across all 5 tickets).

### Ticket T1: Multi-Host State Foundation, Process Daemon Selection & Housekeeping
- **Dependencies**: None (Wave 1 Foundation)
- **Scope**:
  - `herdr_bartender/sanitize.py`
  - `herdr_bartender/intake.py`
  - `herdr_bartender/staging.py`
  - `herdr_bartender/delivery_state.py`
  - `herdr_bartender/cache_schema.py`
  - `herdr_bartender/cache_fields.py`
  - `herdr_bartender/process.py`
  - `herdr_bartender/housekeeping.py`
  - `tests/test_invariants_01_17.py`
  - `tests/test_intake.py`
  - `tests/test_staging.py`
  - `tests/test_process.py`
  - `tests/test_housekeeping.py`
- **Rails (Read-Only)**:
  - `herdr_bartender/hook_guard.sh`
  - `herdr_bartender/salvage.py`
  - `herdr_bartender/markers.py`
  - `tests/test_events.py`
  - `tests/test_pinned_limits.py`
  - `tests/support/**`
- **Acceptance Criteria**:
  1. In `tests/test_invariants_01_17.py:53-56`, change the generated log-shim shebang from `#!/usr/bin/env bash` to `#!/bin/sh` so `test_control_without_disabled_the_same_event_has_effects` completes well under the `0.25s` probe timeout on macOS.
  2. In `herdr_bartender/sanitize.py`, extend `format_agent_name(raw_agent: object, *, remote_host: Optional[str] = None, display_agent: Optional[object] = None) -> str`:
     - Resolve base agent name: `clean = sanitize_agent(raw_agent).lower()`; if `clean in AGENT_NAME_OVERRIDES`, use `AGENT_NAME_OVERRIDES[clean]`; else if `sanitize_agent(display_agent)` is non-empty, use `sanitize_agent(display_agent)`; else if `clean`, use `clean.capitalize()`; else return `f"Herdr ({remote_host})"` when `remote_host` is non-empty or `DEFAULT_AGENT_NAME` (`"Herdr"`).
     - When `remote_host` is non-empty (sanitized via `sanitize_hostname`), use suffix `f" (Herdr \u00b7 {remote_host})"`; otherwise use `" (Herdr)"`. Truncate base name to `AGENT_MAX - len(suffix)` so total length `<= 64`.
  3. In `cache_schema.py` and `cache_fields.py`, add `"remote_unreachable_since"` to `DICT_FIELDS` (`{}` default) and validate its entries as finite numbers (`float`/`int`), dropping malformed keys/values.
  4. In `intake.py`:
     - Add `resolve_event_host(event_data: Mapping, pinned_host: object, current: Callable[[], str] = sanitized_hostname) -> Tuple[str, bool]`: when `event_data.get("host")` is a non-empty string, sanitize with `sanitize_hostname(event_data["host"])` and return `(event_host, event_host != local_host)` where `local_host = resolve_host(pinned_host, current)`; otherwise return `(local_host, False)`.
     - Add `pane_scope_key(canonical_pane: str, host: str, local_host: str) -> str`: return `canonical_pane` when `not host or host == local_host`, else `f"{host}@{canonical_pane}"` (keeping all local keys `"w1:p1"` 100% unchanged while isolating remote keys `"dev-one@w1:p1"`).
     - Scope `session_matches_pane`, `session_matches_tab`, and `session_matches_workspace` by `host` when provided (`(rec.get("host") or local_host) == host`).
  5. In `staging.py` and `delivery_state.py`:
     - Use `resolve_event_host` and `pane_scope_key` for `tombstones`, `agent_exits`, and `pane_generations`.
     - On `new_turn` (`cached.get("desired_state") == "Ended" or "generation" not in cached`), pass `{}` instead of `cached` into `resolve_fields(event_data, context, identity, {} if new_turn else cached)` so a new turn in a pane never inherits stale `cwd`/`title`/`tab_id` from a prior `Ended` turn (while keeping `cached` precedence over `focused_pane_cwd` within a live turn so `tests/test_events.py` passes unchanged).
     - Pass `remote_host=host if is_remote else None` and `display_agent=event_data.get("display_agent")` to `format_agent_name`.
     - Skip local `panes/<hex>` marker writes/removals and `stage_vendor_cleanup` for remote sessions (`host != local_host`).
  6. In `herdr_bartender/process.py`, add `_is_herdr_server(pid: int) -> bool` checking `_ps_field(pid, "args")` for subcommand `"server"` when `not gui and len(candidates) > 1`, preferring `herdr server` over TUI/CLI `herdr` client processes.
  7. In `herdr_bartender/housekeeping.py`, invoke `salvage.prune_corrupt_quarantine(state_dir, now)` inside `sweep_stale_temp_files` and unlink 0-byte regular non-symlink `panes/*.vendor_active` files older than `7 * 86400` seconds.
  8. Add unit tests in `tests/test_intake.py`, `tests/test_staging.py`, `tests/test_process.py`, and `tests/test_housekeeping.py` verifying all new behaviors.

---

### Ticket T2: Antigravity Standalone Hook Hardening & Turn Lifecycle
- **Dependencies**: `T1`
- **Scope**:
  - `scripts/agy-notify-hook.sh`
  - `tests/test_agy_notify_hook.py`
- **Rails (Read-Only)**:
  - `herdr_bartender/**`
  - `tests/support/**`
- **Acceptance Criteria**:
  1. In `scripts/agy-notify-hook.sh:7-12`, when `AGY_HOOK_EVENT` and `$1` are empty, extract top-level camelCase `"hookEventName"` first from `$RAW_INPUT`, falling back to snake_case `"hook_event_name"`.
  2. Emit valid Antigravity stdout JSON for all hook events before exiting:
     - `PreToolUse`: set `OUTPUT_EMITTED=1`, write `'{"decision":"allow"}\n'`, and `exit 0` immediately (<50ms, zero network/dismissal calls).
     - `PostToolUse` | `SessionStart`: set `OUTPUT_EMITTED=1`, write `'{}\n'`, and `exit 0` immediately.
     - `PreInvocation` | `PostInvocation`: active status events; `emit_lifecycle_response` writes `'{}\n'`.
     - `Stop` | `SessionEnd`: active status events; `emit_lifecycle_response` writes `'{"decision":""}\n'`.
     - Unknown/arbitrary event names: set `OUTPUT_EMITTED=1` and `exit 0` immediately with empty stdout and zero network calls.
  3. Pass `CANONICAL_PANE="${CANONICAL_PANE:-}"` into the Python payload builder and implement accurate turn state mapping:
     - Extract `prompt_title = sanitize_str(d.get("lastUserInput"), 120)` when `isinstance(d.get("lastUserInput"), str)` else `""`.
     - `PreInvocation`: `state = "Working"`, `title = prompt_title or "Thinking..."`.
     - `PostInvocation`: `state = "Working"`, `title = prompt_title` (and bash no-Python fallback `FB_STATE="Working"`), eliminating mid-turn `Working -> Idle -> Working` flicker.
     - `SessionEnd`: `state = "Ended"`, `title = ""` in both Herdr fail-open and standalone modes, retiring `.vendor_active` / `.active` on HTTP `200`/`404`/`410`.
     - `Stop`:
       - When `CANONICAL_PANE` is non-empty (Herdr pane fail-open fallback): preserve `state = "Ended"`, `title = ""`, and `.vendor_active` retirement on HTTP `200`/`404`/`410`.
       - When `CANONICAL_PANE` is empty (standalone `agy` CLI mode):
         - If `d.get("fullyIdle") is False`: `state = "Working"`, `title = prompt_title or "Background task running..."`.
         - Else if `d.get("terminationReason") == "ERROR"` or `bool(d.get("error"))`: `state = "Waiting"`, `err_val = d.get("error")`, `title = sanitize_str(err_val, 120) if isinstance(err_val, str) and sanitize_str(err_val, 120) else (prompt_title or "Error")`.
         - Else: `state = "Done"`, `title = prompt_title` (and bash fallback `FB_STATE="Done"` when `CANONICAL_PANE` is empty).
         - Preserve/update `standalone/<hex>.active` on standalone `Stop` (do NOT retire `.active` until `SessionEnd` or `sweep_standalone_active` PID exit), while continuing to reuse `PREV_VENDOR_SID` when `conversationId` is absent.
  4. Update and expand `tests/test_agy_notify_hook.py` to verify all new contracts while keeping all Herdr fail-open `HERDR_PANE_ID` tests passing.

---

### Ticket T3: First-Party `agy-notify-hook.sh` Installer & Integrity Check
- **Dependencies**: `T1`, `T2`
- **Scope**:
  - `herdr_bartender/agy_hook.py` (new)
  - `herdr_bartender/hooks_install.py`
  - `herdr_bartender/hooks_integrity.py`
  - `herdr_bartender/hooks.py`
  - `tests/test_agy_hook_install.py` (new)
- **Rails (Read-Only)**:
  - `scripts/agy-notify-hook.sh`
  - `tests/test_hooks_install.py`
  - `tests/test_hooks_integrity.py`
  - `tests/support/**`
- **Acceptance Criteria**:
  1. Keep `HOOK_NAMES = ("claude-event-hook.sh", "codex-notify-hook.sh")`, `vendor-hook-sha.json`, and `check_hook_integrity().hooks` unchanged so existing vendor-hook allowlist and integrity tests pass untouched.
  2. Create `herdr_bartender/agy_hook.py`:
     - `AGY_HOOK_NAME = "agy-notify-hook.sh"`, reading source bytes from `repo_root() / "scripts" / AGY_HOOK_NAME`.
     - `install_agy_hook(hooks_dir: Optional[Path] = None) -> bool`: validates syntax with `hooks_fs.bash_syntax_ok` and atomically writes `hooks_dir / "agy-notify-hook.sh"` with mode `0o755` when `hooks_dir` exists.
     - `uninstall_agy_hook(hooks_dir: Optional[Path] = None) -> bool`: unlinks `hooks_dir / "agy-notify-hook.sh"` if missing or if it matches `scripts/agy-notify-hook.sh` / contains `HERDR_PLUGIN_STATE_DIR`.
     - `verify_agy_hook_intact(hooks_dir: Optional[Path] = None) -> Tuple[bool, str]`: returns `(True, "intact")` iff `hooks_dir / "agy-notify-hook.sh"` is a regular executable file matching `scripts/agy-notify-hook.sh` byte-for-byte, else `(False, reason)` (`"missing"`, `"outdated_or_stub"`, `"not_executable"`, `"hooks_dir_missing"`).
     - `audit_agy_hooks_config(config_path: Optional[Path] = None) -> Tuple[str, ...]`: read-only inspection of `${HOME}/.gemini/config/hooks.json` (when present as a regular file `<= 256 KiB`), returning warning strings if `PreToolUse` still invokes `agy-notify-hook.sh`. Never mutates user config files.
  3. In `hooks_install.py`:
     - In `install_hooks(state_dir, hooks_dir)`: after `load_template_bytes()` succeeds, when `hook_files` is non-empty OR `(hooks_dir / AGY_HOOK_NAME).exists()`, also run `install_agy_hook(hooks_dir)` (preserving `False` return when `hooks_dir` is missing or empty, and never touching files if `load_template_bytes()` fails).
     - In `uninstall_hooks(state_dir, hooks_dir)`: when `hooks_dir.is_dir()`, also run `uninstall_agy_hook(hooks_dir)`.
     - Re-export `install_agy_hook`, `uninstall_agy_hook`, `verify_agy_hook_intact`, and `audit_agy_hooks_config` from `herdr_bartender/hooks.py`.
  4. Add comprehensive unit tests in `tests/test_agy_hook_install.py`.

---

### Ticket T4: Herdr `0.9.5` `pane.moved` & `pane.exited` Event Handlers
- **Dependencies**: `T1`
- **Scope**:
  - `herdr-plugin.toml`
  - `herdr_bartender/handlers/__init__.py`
  - `herdr_bartender/handlers/pane_close.py`
  - `herdr_bartender/handlers/pane_move.py` (new)
  - `tests/test_pane_move_exit.py` (new)
- **Rails (Read-Only)**:
  - `herdr_bartender/intake.py`
  - `herdr_bartender/staging.py`
  - `herdr_bartender/handlers/flow.py`
  - `tests/support/**`
- **Acceptance Criteria**:
  1. Add `pane.moved` and `pane.exited` `[[events]]` blocks to `herdr-plugin.toml`.
  2. In `herdr_bartender/handlers/pane_close.py`, add `handle_pane_exited` (`event_name = "pane.exited"`), parameterized cleanly over `stage_pane_close_result` so `"event": "pane.exited"` is emitted on close payloads while tombstoning, removing the pane marker, and staging vendor cleanup.
  3. Create `herdr_bartender/handlers/pane_move.py` (`handle_pane_moved`):
     - Consumes Herdr `0.9.5` `pane.moved` payload (`previous_pane_id`, `previous_workspace_id`, `previous_tab_id`, `pane` `PaneInfo` object).
     - In one Step A transaction via `handlers.flow.run_event`:
       - When normalized `previous_pane_id` differs from destination canonical `pane.pane_id`, stages `stage_pane_close_result` on `previous_pane_id` and removes its pane marker.
       - When destination `pane` has a valid `pane_id` and an active agent (`pane.get("agent")` non-empty or transferred from a live session on `previous_pane_id`), stages `stage_status` onto destination `pane.pane_id` with `cwd = pane.get("foreground_cwd") or pane.get("cwd")`, `title = pane.get("title") or pane.get("label") or pane.get("terminal_title_stripped")`, `tab_id = pane.get("tab_id")`, `agent`, `display_agent`, and `agent_status`.
     - Export `handle_pane_exited` and `handle_pane_moved` in `herdr_bartender/handlers/__init__.py`.
  4. Add unit tests in `tests/test_pane_move_exit.py` covering cross-workspace pane moves, same-id moves, non-agent pane moves, and `pane.exited`.

---

### Ticket T5: Local Snapshot Self-Healing, Remote SSH Machine Watcher & CLI Diagnostics
- **Dependencies**: `T1`, `T3`, `T4`
- **Scope**:
  - `herdr_bartender/herdr_client.py` (new)
  - `herdr_bartender/remote_watcher.py` (new)
  - `herdr_bartender/background.py`
  - `herdr_bartender/reconciler.py`
  - `herdr_bartender/schedule.py`
  - `herdr_bartender/cli.py`
  - `tests/test_remote_watcher.py` (new)
  - `tests/test_snapshot_sync.py` (new)
- **Rails (Read-Only)**:
  - `herdr_bartender/intake.py`
  - `herdr_bartender/staging.py`
  - `herdr_bartender/agy_hook.py`
  - `herdr_bartender/handlers/**`
  - `tests/test_reconciler.py`
  - `tests/test_hooks_integrity.py`
  - `tests/support/**`
- **Acceptance Criteria**:
  1. Create `herdr_bartender/herdr_client.py` (never called inside `runtime.IN_CRITICAL_SECTION`; assert `not runtime.IN_CRITICAL_SECTION`):
     - `discover_enabled_remotes(xdg_state_home: Optional[Path] = None, home: Optional[Path] = None) -> Tuple[RemoteEndpoint, ...]`: reads `${XDG_STATE_HOME:-${HOME}/.local/state}/herdr/client/endpoints.json` via `boundedio.read_regular_file` (returns `()` with zero subprocess spawns when absent). Extracts `ssh` items with `"enabled": True`, deriving `host = sanitize_hostname(label or target or id)`.
     - `fetch_herdr_snapshot(machine: Optional[str] = None, timeout: float = 2.5) -> Optional[dict]`: runs `["herdr", "api", "snapshot"]` or `["herdr", "--machine", machine, "api", "snapshot"]` with `stdin=subprocess.DEVNULL` and `LC_ALL=C`; returns `parsed["result"]["snapshot"]` only when exit code is `0` and `result.snapshot` is a `dict` with `"panes"` as a `list` (returns `None` when stdout is empty, as in `tests/support/shims/herdr`, or on timeout/error).
  2. Create `herdr_bartender/remote_watcher.py` (pure under-lock mutations over pre-fetched snapshots):
     - `sync_local_snapshot(data, snapshot, now_wall, now_ns)`: refreshes live local sessions' `cwd` (`foreground_cwd or cwd`), `title`, `tab_id`, and `agent_status` when changed (bumping `seq` and rebuilding `desired_payload` only on real change), and closes cached local live sessions whose `pane_id` disappeared from `snapshot["panes"]` (provided `admitted_at_ns` predates snapshot fetch). No-op when `snapshot is None`.
     - `sync_remote_endpoint(data, endpoint, snapshot_or_none, now_wall, now_ns, herdr_alive)`:
       - When `snapshot_or_none is None`: stamps `data["remote_unreachable_since"].setdefault(endpoint.host, now_wall)`; holds existing `herdr:<endpoint.host>:*` cards steady for `<= 300.0`s, and expires them (`reason="remote_unreachable"`) once `> 300.0`s.
       - When `snapshot_or_none` is valid: clears `remote_unreachable_since[endpoint.host]`; stages/updates active agent panes (`herdr:<endpoint.host>:<canonical_pane>`, `agent` formatted as `<Agent> (Herdr · <endpoint.host>)`, `terminal: "Herdr"`) bumping `seq` **only** when state, agent, title, or cwd changed; stages `Ended` for cached live sessions on `endpoint.host` whose pane disappeared or whose agent exited.
  3. Wire into `background.py`, `reconciler.py`, and `schedule.py`:
     - Pre-fetch local snapshot (throttled every `10.0s` when live local sessions exist) and remote snapshots (every `3.0s` when `discover_enabled_remotes()` is non-empty) **outside** `active-sessions.lock`, then apply under the lock before `deliver_due_sessions`.
     - When `discover_enabled_remotes()` is empty (`()`), preserve exact existing `_idle_now` and `next_wake` behavior so `run_reconcile_background()` idles out cleanly in tests and single-machine setups; when enabled remotes exist and Herdr is alive, poll on a `3.0s` cadence.
  4. Wire into `herdr_bartender/cli.py`:
     - Add `"pane.moved": handle_pane_moved` and `"pane.exited": handle_pane_exited` to `EVENT_HANDLERS` (and `intake.EVENT_NAMES` if checked, or `EVENT_HANDLERS` in `cli.py`).
     - Add `--install-agy-hook` CLI flag, include `agy_hook_intact` / `agy_hook_status` / `agy_hooks_json_warnings` in `--health`, and surface `agy` warnings + Bartender `/health` session-count drift in `--status` **after** the `HOOK_NEEDS_REVIEW` first line.
  5. Add unit tests in `tests/test_remote_watcher.py` and `tests/test_snapshot_sync.py`.
