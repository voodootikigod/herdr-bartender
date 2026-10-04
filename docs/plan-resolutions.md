# Plan Resolutions (Normative Amendments to `herdr-bartender-plan.md`)

The 2026-10-04 audit (`docs/audit/gaps.json`) found places where the specification contradicts itself.
Each entry below records the reading the implementation follows. Where this file and the plan disagree, this file wins.

| ID | Plan refs | Contradiction | Resolution |
| :--- | :--- | :--- | :--- |
| R1 | L10 vs L13/L190/L249 | Colon-less pane IDs are rejected when the event has no workspace_id, yet `HERDR_WORKSPACE_ID` is also listed as a fallback | `normalize_pane_id` uses `ws = workspace_id or os.environ.get("HERDR_WORKSPACE_ID")` and returns `""` when it is empty. Handlers reject only an empty canonical ID. |
| R2 | L190 vs L11 | event `workspace_id` vs colon-qualified prefix precedence | When the canonical pane contains `:`, its prefix is authoritative for the stored `workspace_id`. A mismatched event `workspace_id` is logged and ignored. The code never falls back to `"default"`. |
| R3 | L161 vs L181 | Focus test for focused-agent admission | Admission uses `is_focused` from §2.3: `focused_id in (canonical_pane, raw_pane)`. |
| R4 | L263 vs L435 | `last_event_ns` clamp | `last_event_ns = max(arr_ns, prev + 1)`. `last_arrival_ns` stays the raw arrival time. |
| R5 | L471 vs L531/L553 | Synchronous Step B loop | The synchronous handler is capped at 1 session and 2 POSTs. If a newer seq arrives during the send, the handler clears the lease, touches `reconciler.pending` and ensures the reconciler is running instead of looping. Compensating and vendor-dismiss POSTs fall outside the cap but are still gated on `time_remaining() > 0.3`. |
| R6 | L390 vs L398/L738 | Envelope payload key | Spool envelopes use `event_data` (plus `event_name`, `context`, `arrival_ns`, `enqueued_ns`). |
| R7 | L91/L398/L577 | Tombstone `last_source_timestamp` | `max(session.last_source_timestamp, event timestamp)`. |
| R8 | L575 vs L397 | Close-envelope generation check | Skip the close only when `session.generation > envelope.generation`. |
| R9 | L713 vs L390 | Lock budget | The table uses 200ms for lock acquisition. Worst case is 1235ms, still under 1.5s. |
| R10 | §4.3 vs §6.1 | Orphan lock mode | Event and plugin paths use `LOCK_NB` with a 50ms deadline and leave the work to the reconciler on contention. `--replay-orphans` and `--cleanup` may block, bounded by their own budget. |
| R11 | L645 vs L646/L43/L56/#65 | Dismissal retry cap | 5 attempts at t=0,2,4,6,8 (2.0s interval). Purge on HTTP 200, after 5 attempts, or once age ≥ 10s. The 60s-age clause is a hard cap for malformed entries without timestamps. |
| R12 | L230-235/L680 | Orphaned session eviction | Evict when the session is more than 12h past its TTL, at the terminal absence horizon, or under 256-cap pruning. |
| R13 | L58/L834 vs L667 | Heartbeat during Bartender-absent backoff | The 20s marker heartbeat keeps running during the 300s absence backoff. Only the delivery sweep backs off. |
| R14 | §1 L113/L133 vs §5.1 items 8-9 | Restart detection | A restart is a PID change with the old PID confirmed dead, or a start-time change on the same PID. Both start times must be known (non-None). A failed `ps` lookup never counts as a restart. |
| R15 | §7.1 L800 vs §7.2 block | Herdr liveness probe in the guard | The guard uses `pgrep -xi herdr` (plus the tighter `pgrep -f '/Herdr.app/Contents/MacOS/'`). It no longer uses the loose `pgrep -f Herdr.app`. Python `is_herdr_alive()` uses the same rule. |
| R16 | §8 vs §7.2 | Guard file permissions | The guard keeps `umask 077` for every file it writes, including `.vendor_active` and the splice FIFO, and restores the caller's umask before the block ends. |
| R17 | §7.1 prose vs block | Guard minutiae | `command -v perl` (portable). A bare touch is upgraded to a UUID record (documented behavior). Uninstall preserves `orig_mode` exactly. Marker mtime uses a portable `stat` (GNU `-c %Y` and BSD `-f %m`), validated as numeric. |
| R18 | §10.1 L1203 | Invariant count | 68 numbered invariants are normative. |
| R19 | §3.3 vs vendor dismissal | `agent` value on vendor-UUID `Ended` | `"Herdr"`, the same as the minimal Ended payload. |
| R20 | §3.4 L243 | Invocation contract | stdin JSON envelope `{event,data,context}` is primary. The event name comes from `envelope.event`, then argv[1] (if not a flag), then legacy `HERDR_PLUGIN_EVENT*` env vars. With empty or malformed stdin and a known argv event, the plugin does a safe no-op plus `ensure_reconciler_running()`. |
| R21 | manifest | Startup hook | The manifest no longer runs `--cleanup` at startup, because that bypasses DISABLED and would breach the 2.0s budget. The startup hook runs `--reconcile-background`, which detaches and handles restart detection. Event commands pass the event name in argv. |
| R22 | README vs §8 | Remote hosts | Remote SSH forwarding and `NOTCHBAR_AGENTS_HOST` are out of scope for v1.0. The README documents loopback only. |
| R23 | §6.1 L245 vs L727 | Signal handler I/O | The SIGALRM handler only sets flags. All I/O happens after the handler returns: at a critical-section exit, or in the main flow's watchdog checkpoints. |
