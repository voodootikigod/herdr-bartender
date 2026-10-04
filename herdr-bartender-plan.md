# Herdr to Bartender Pro (Top Shelf) Integration Specification

## 1. Executive Summary

This specification defines the architecture, protocol contracts, data normalization, concurrency guarantees, and failure recovery mechanisms for synchronizing **Herdr** pane notifications and agent lifecycle events into **Bartender Pro's Top Shelf** (NotchBar AI Agent bridge) on macOS.

Key architectural decisions:
- **Native Herdr Plugin Architecture**: Implemented as `herdr-bartender` in `~/Projects/herdr-bartender` and symlinked into `~/.config/herdr/plugins/herdr-bartender`.
- **Marker Touching on Confirmed Delivery & First-Turn Fallback Healing**: Pane markers in `$STATE_DIR/panes/<hex>` are touched **strictly upon confirmed HTTP 200 OK delivery** to Bartender Pro (Step C). Step A does NOT touch markers on admission, avoiding false-positive health signals during bridge outages. If a vendor hook fires during the initial turn before Herdr's first delivery completes, the vendor hook falls through and records `.vendor_active` with its native session UUID. Upon Herdr's first confirmed delivery or upon pane close, `cleanup_vendor_active` unlinks `.vendor_active` (including bare touches) to actively restore deduplication, sending `Ended` to Bartender for any recorded vendor UUID to dismiss the stranded vendor entry. In the hook guard, a bare touch passes through turn-terminal events when Herdr is unhealthy, while non-terminal events under healthy Herdr unlink the bare touch and exit 0 (suppress). Network I/O is **strictly forbidden under the file lock** (`assert not IN_CRITICAL_SECTION`); dismissals are staged under lock and dispatched outside the lock.
- **Single Canonical Wire Session ID & Pane Normalization**: Formatted as `herdr:{sanitized_host}:{canonical_pane_id}`. In Herdr 0.8.x, terminal panes run inside workspace context `W`. Herdr injects `HERDR_WORKSPACE_ID=W` into the pane shell environment and emits `event.data.workspace_id = W` in event payloads. Verified Herdr fact: colon-less pane IDs without workspace_id in event data are invalid and rejected to prevent `default:p1` vs `w1:p1` divergence. Canonical pane derivation is **strictly unified across Python and Bash**:
  - If `pane_id` contains `:`, it is already workspace-qualified (e.g. `w1:p1`) and is used directly without prefixing.
  - If colon-less (e.g. `p1`), the Bash hook guard in the pane shell computes `${HERDR_WORKSPACE_ID:+${HERDR_WORKSPACE_ID}:${HERDR_PANE_ID}}`. If `HERDR_WORKSPACE_ID` is empty or unset, it derives `""` and safely fails open to vendor script.
  - In Python, `normalize_pane_id(pane_id, workspace_id)` derives `ws = workspace_id or os.environ.get("HERDR_WORKSPACE_ID")`. If `ws` is empty or None, it returns `""` and the plugin process drops the invalid event. Otherwise it computes `f"{ws}:{raw_pane}"`.
  - Both Bash and Python derive the exact same canonical string `${ws}:${raw_pane}`, mapping 1:1 to lowercase hex encoding. There is no alias, no symlink, and no dual-linking.
  - Total wire `session_id` length is strictly bounded to 96 characters (`^[a-zA-Z0-9_:-]{1,96}$`). Hook guards and marker operations operate strictly on injective lowercase hex encoding of the canonical pane ID, eliminating cross-workspace marker collisions.
- **Injective Hex Filesystem Marker Encoding**: File paths representing pane IDs (in `$STATE_DIR/panes/`) use lowercase hex encoding computed identically in Python (`canonical_pane_id.encode('utf-8').hex()`) and Bash (`printf '%s' "$CANONICAL_PANE" | od -An -tx1 | tr -d ' \t\n'`), e.g. `default:p1` &rarr; `64656661756c743a7031`. Hex encoding of an empty string evaluates to `""`. Guarantees collision-free, injective, case-safe filesystem storage for markers (`<hex>`), failure flags (`<hex>.failed`), and vendor active flags (`<hex>.vendor_active`).
- **Confirmed-Delivery-Gated Dedup Guard & Bidirectional Handover Protocol**:
  - The standalone vendor hooks (`claude-event-hook.sh`, `codex-notify-hook.sh`) suppress native vendor notifications for panes managed by Herdr **if and only if ALL** of the following 7 conditions hold:
    1. Integration is not disabled (`$STATE_DIR/DISABLED` absent).
    2. Bridge delivery is not globally down (`$STATE_DIR/DELIVERY_DOWN` absent).
    3. `$HERDR_PANE_ID` matches `^[a-zA-Z0-9_:-]{1,48}$`.
    4. Per-pane marker `$STATE_DIR/panes/<hex>` exists.
    5. Per-pane marker is **fresh** (mtime updated within the last 60s).
    6. Per-pane failure flag `$STATE_DIR/panes/<hex>.failed` is absent.
    7. Herdr process is actively running (`pgrep -xi "herdr"`).
  - **Bidirectional Handover, Bounded Stdin Capture & Stranded Entry Prevention**:
    - When Herdr delivery fails (marker stale, `.failed` exists, or `DELIVERY_DOWN` active), the vendor hook falls through to notify Bartender under its CLI native session UUID and creates `$STATE_DIR/panes/<hex>.vendor_active` containing `{"vendor_session_id": "<uuid>"}`.
    - **Bounded Fail-Open Stdin Capture with Replay-plus-Remainder Splice**: Captured via `/usr/bin/perl -e '$SIG{ALRM} = sub { exit 142 }; alarm 1; while (sysread(STDIN, my $buf, 65536)) { print $buf; } alarm 0; exit 0;'` (with python3 fallback), strictly bounding whole-capture latency to <=1.0s without hanging on stalled or open pipes. Large payloads (>64KB, up to arbitrary size within 1.0s) are captured byte-exact without truncation (64KB is chunk buffer size). If capture times out (`_HB_CAPTURE_ERR -ne 0`), the hook guard checks if any bytes were captured in `_HB_GUARD_TMP`: if bytes exist, it splices the captured prefix with the remainder of stdin via an unlinked temporary FIFO and background process:
      ```bash
      _HB_SPLICE_FIFO=$(mktemp -u "$_HB_STATE_HOME/.guard_splice.XXXXXX" 2>/dev/null || true)
      if [ -n "$_HB_SPLICE_FIFO" ] && mkfifo "$_HB_SPLICE_FIFO" 2>/dev/null; then
        ( cat "$_HB_GUARD_TMP"; rm -f "$_HB_GUARD_TMP" 2>/dev/null || true; exec cat ) > "$_HB_SPLICE_FIFO" 2>/dev/null &
        exec < "$_HB_SPLICE_FIFO"
        rm -f "$_HB_SPLICE_FIFO" 2>/dev/null || true
      fi
      ```
      This guarantees zero data loss on timed-out pipes while using a background job and unlinked FIFO avoids bash waiting for child process substitutions on shell exit. If `_HB_GUARD_TMP` is empty, it unlinks the file and lets stdin pass through untouched. In the pure bash fallback (when neither perl nor python3 is available), bash avoids reading or buffering stdin; instead, bash clears `_HB_GUARD_TMP=""` and classifies using argv `$1` only, leaving stdin completely untouched for the vendor script.
    - **Top-Level UUID Extraction, Bare-Touch Upgrade & Atomic `.vendor_active` Lifecycle**: The guard extracts top-level `session_id` using `grep -m1` (supporting both stdin JSON and argv `$1` JSON), validates against `^[a-zA-Z0-9_-]{16,64}$`, and writes `.vendor_active` atomically via temporary file (`mktemp`) and atomic rename (`mv -f`). When a bare touch exists (no UUID captured on first turn), any subsequent vendor turn with an extractable `session_id` immediately upgrades the bare touch to a UUID record (`{"vendor_session_id": "<uuid>"}`). **No Blind Expiry**: The guard does NOT blindly unlink `.vendor_active` based on mtime, preventing long agent turns or idle periods (>60s) from destroying handover tracking. Whenever passing through to the vendor hook for non-session-terminal events, the guard refreshes mtime via `touch "$_HB_VENDOR_ACTIVE" 2>/dev/null || true`. While Herdr is dead, `.vendor_active` is NEVER swept by the reconciler based on mtime.
    - **Turn-Terminal vs Session-Terminal Separation & Stranded Vendor Entry Prevention**:
      - Inspects argv `${1:-}` (including OpenAI Codex style argv JSON payload) and stdin JSON payload (`hook_event_name`, `event`, `state`, or `type`):
        - Session-terminal events: `Ended`, `SessionEnd`, `session-end`.
        - Turn-terminal events: `Stop`, `Done`, `AgentDone`, `AgentWaiting`, `agent-turn-complete`.
      - **Bounded Vendor Dismissal Guarantee**: Vendor session-terminal events (`Ended`, `SessionEnd`, `session-end`) are **NEVER suppressed by the hook guard**, regardless of whether `.vendor_active` exists or whether Herdr is healthy; on session-terminal events, the hook unlinks `.vendor_active` and passes through to vendor. Because Top Shelf HTTP updates and hook guard executions are asynchronous, an unlocked race between guard pass-through and plugin vendor dismissal could theoretically leave a transient vendor entry. The system guarantees bounded vendor dismissal: when `cleanup_vendor_active` sends `Ended` to Bartender for a vendor UUID, the UUID is actively tracked in `dismissed_vendor_uuids` across a 10-second settling window (resending `Ended` every 2s, up to 5 attempts, e.g. at t=0, 2, 4, 6, 8, 10s) until confirmed or after 5 attempts/10s elapsed. If the vendor CLI is killed abruptly (e.g. SIGHUP on pane close) without emitting `SessionEnd` before any turn supplied a session UUID, the unknown vendor UUID on Top Shelf remains until Bartender restart or session dismissal. For all turns where UUID was captured or `SessionEnd` is emitted, Top Shelf is cleanly cleared.
      - If `.vendor_active` exists:
        - **Bare-Touch Recovery & Dedup Restoration**: If `.vendor_active` is a bare touch (no UUID extracted):
          - If Herdr is healthy (`_HB_HERDR_HEALTHY -eq 1`), non-terminal events unlink `.vendor_active` (`rm -f "$_HB_VENDOR_ACTIVE"`) and suppress (`exit 0`), actively restoring deduplication.
          - Turn-terminal events (`Stop`, `Done`, `AgentDone`, `AgentWaiting`, `agent-turn-complete`) pass through to the vendor script without unlinking `.vendor_active`.
          - Session-terminal events (`Ended`, `SessionEnd`, `session-end`) pass through to the vendor script and unlink `.vendor_active`.
        - If `.vendor_active` contains an extracted UUID:
          - Session-terminal (`Ended`, `SessionEnd`, `session-end`): unlinks `.vendor_active` and passes through to dismiss the vendor's Top Shelf entry.
          - Turn-terminal (`Stop`, `Done`, `AgentDone`, `AgentWaiting`, `agent-turn-complete`): passes through to vendor to update terminal turn state, preserving `.vendor_active`, while Herdr plugin cleans up vendor active upon confirmed delivery.
          - Non-terminal: checks the 7-condition Herdr health predicate; suppresses vendor if Herdr recovered, otherwise maintains `.vendor_active`.
      - If `.vendor_active` is absent:
        - Session-terminal (`Ended`, `SessionEnd`, `session-end`): passes through directly to vendor script to dismiss any raced vendor entries.
        - Turn-terminal and Non-terminal: if Herdr is healthy (all 7 conditions met), suppresses vendor hook (`exit 0`); if unhealthy, passes through to vendor (recording `.vendor_active` atomically).
    - **Vendor Dismissal Queue in Cache with Fallback Cancellation & Re-Queue**: When `cleanup_vendor_active` sends `Ended` to Bartender for a vendor UUID, it records the UUID in `dismissed_vendor_uuids: { "<uuid>": { "timestamp": <ts>, "pane_hex": "<hex>", "attempts": 1, "last_attempt": <ts> } }` (capped at 64 entries in cache, pruning oldest) with retries across a 10-second settling window (retrying every 2s, up to 5 attempts, e.g. at t=0, 2, 4, 6, 8, 10s). Purged once >=10s has elapsed or 5 attempts have been made, or if entry age > 60s. During each reconciler sweep, before re-sending `Ended`, the reconciler checks if vendor fallback is active or integration disabled: if `$STATE_DIR/DISABLED` exists, or `NO_HOOKS` exists, or `$STATE_DIR/panes/<pane_hex>.vendor_active` exists, or `.failed` exists, or `not is_herdr_alive()`: the queued dismissal is immediately CANCELLED and purged without sending `Ended`, protecting active vendor fallback representations. If cancelled due to `.vendor_active`, on subsequent confirmed Herdr delivery on that pane, `cleanup_vendor_active` re-reads `.vendor_active` and re-queues the dismissal in `dismissed_vendor_uuids` until confirmed gone.
    - If the user closes the pane in Herdr, `remove_pane_marker` unlinks marker and failed flag under lock; after lock release, `cleanup_vendor_active(canonical_pane, is_pane_closed=True)` sends `Ended` for any recorded `vendor_session_id` and unlinks `.vendor_active` (including bare touches on confirmed delivery and pane close).
  - **Reconciler Watchdog Heartbeat Gated on Process Liveness**: While any non-salvaged session is delivered in cache (`desired_state != "Ended"` and `delivery_status == "delivered"`, including `Working`, `Waiting`, `Idle`, and `Done`) and Herdr process is actively running (`is_herdr_alive()`), the background reconciler runs on a **20-second cadence**, updating marker freshness for all live panes with confirmed delivery so panes idle between turns (>60s) never suffer premature fall-through. If Herdr process exits (dead), marker refreshing ceases immediately and vendor hooks fall through within 60s.
  - On the **very first delivery failure** for a pane, Herdr touches `<hex>.failed`, enabling immediate fall-through to vendor hooks without waiting for retry exhaustion.
  - On 3 consecutive global bridge communication failures, `$STATE_DIR/DELIVERY_DOWN` is touched.
- **Client-Side Best-Effort Serialized Lease Protocol with Post-Takeover Convergence & Guarded Stale Send Compensation**:
  - Bartender 6.0.4 NotchBar bridge accepts JSON payloads at `POST /event` and renders the last-received state. Server-side `seq` enforcement is not assumed or claimed.
  - Non-overlapping deliveries are managed via a client-side serialized lease protocol with post-takeover convergence:
    - Each session maintains an integer `seq`, `delivered_seq`, `rejected_seq`, and monotonic `generation`.
    - Senders operate under a per-session lease (`lease_token`, `lease_deadline = time.time() + 1.5`, `sending_pid`).
    - **PID Reuse Defense**: Lease token includes the sender's integer epoch process start time: `my_token = f"{os.getpid()}:{PROCESS_START_TIME}:{now}:{sid}"`. Standardized lease duration is 1.5s across all code paths. Liveness checks verify `is_process_instance_alive(sending_pid, holder_start_time)`, comparing both PID and start time to immediately claim leases if a PID was reused by another process.
    - **Leases Are Never Held Across Sleeps**: Senders and background helpers do NOT hold leases across retry backoff sleeps. Leases are released before sleeping and re-claimed under lock upon waking. Senders verify the lease token before initiating network transmission in Step B.
    - **Step C Supersession Re-Sync & Guarded Stale Send Compensation**:
      - If a sender discovers in Step C that its `lease_token != my_token`, it forces a re-sync: sets `delivered_seq = 0`, `delivery_status = "in_flight"`, touches `$STATE_DIR/reconciler.pending`, ensures the background reconciler is running, and exits without overwriting newer state. Deferral paths also touch `reconciler.pending` and ensure the reconciler is running.
      - **Guarded Stale Send Compensation**: If non-Ended HTTP delivery succeeded in Step B but in Step C `session is None` (e.g. concurrent cascade eviction) or `canonical_pane in tombstones`, the sender stages compensation. **Under-Lock Re-Verification**: Before transmitting compensating `Ended`, the sender re-acquires the cache lock to check if `active_s = data.get("sessions", {}).get(session_id)` exists with `desired_state != "Ended"`; if active, compensation is **aborted** immediately so live re-admitted sessions are never dismissed. If compensation proceeds and transmits `Ended`, the sender immediately re-acquires the lock; if a live session was admitted during transmission, it forces `delivered_seq = 0`, `delivery_status = "in_flight"`, touches `reconciler.pending`, and spawns the reconciler to re-assert active state on Bartender.
    - **Canonical Lease Takeover Truth Table (All 6 Rows)**:
      | Row | `is_process_instance_alive(holder_pid, holder_st)` | `now vs deadline` | Action | Rationale |
      | :--- | :--- | :--- | :--- | :--- |
      | 1 | `None` (unclaimed) | Any | **CLAIM** | No active sender. Claim lease and proceed to Step B. |
      | 2 | Caller is `holder_pid` | Any | **CLAIM** | Re-entrant within same process; advance deadline. |
      | 3 | `False` (dead or reused PID) | Any | **CLAIM** | Prior process crashed or PID was reused. Take over immediately with new token and deadline = now + 1.5s. |
      | 4 | `True` (alive) | `now < deadline` | **DEFER** | Prior process actively in-flight. Stage desired state, touch `reconciler.pending`, defer transmission. |
      | 5 | `True` (alive) | `deadline <= now < deadline + 0.5s` | **DEFER** | Socket drain grace window. Stage desired state, touch `reconciler.pending`, defer to reconciler. |
      | 6 | `True` (alive) | `now >= deadline + 0.5s` | **CLAIM** | Prior process hung beyond deadline + grace. Overwrite lease with new token; hung attempt will abort via token verification in Step C. |
    - **Cascade Clamping & Inline Budgeting**: Container cascades (`tab.closed`, `workspace.closed`) clamp synchronous deliveries to **at most 1 session inline (<200ms)**, spilling all remaining sessions (>1) immediately to the detached background reconciler so the 1.5s process watchdog is never breached.
    - Sockets use bounded timeouts clamped to `min(0.2, max(0.05, time_remaining() - 0.3))` (<200ms per POST). Senders verify lease tokens before and after HTTP I/O.
    - Lock acquisition deadline is dynamically scaled to `time.monotonic() + min(0.2, max(0.02, time_remaining() - 0.3))`. Post-lock operations (compensation transmission, vendor cleanup) are gated on `time_remaining() > 0.3s`; if insufficient budget remains, deferred to reconciler via `pending_compensations` / `pending_vendor_cleanups`.
- **Monotonic Generation Ordering, Spool Protection & Hardened Tombstones**:
  - `PROCESS_ARRIVAL_TIME_NS = time.time_ns()` is captured at top-level process entry before any lock wait and passed to all handlers.
  - Timestamps across the system (`admitted_at_ns`, `last_event_ns`, `arrival_ns`, `closed_at_ns`) use nanosecond wall-clock epoch integers (`time.time_ns()`), alongside monotonic integer counters `seq`, `generation`, and root-level `cache_seq`. Nanosecond timestamps enforce non-regression: `last_event_ns = max(now_ns, session.get("last_event_ns", 0) + 1)`.
  - Stable `closed_at_ns` origin is recorded once at close admission and preserved through tombstone creation, preventing timestamp drift.
  - **Monotonic Root Counter `next_generation` Invariant**: `active-sessions.json` maintains `next_generation: <int>` at the root alongside `pane_generations: { "<canonical_pane>": <int> }`. Advancing a generation computes `curr_gen = max(next_generation, pane_generations.get(canonical_pane, 0), session.get("generation", 0)) + 1` and updates `data["next_generation"] = curr_gen`. Even if an inactive pane key is pruned from `pane_generations`, the global monotonic root counter guarantees that any future agent turn on that pane receives a generation strictly greater than all prior generations in history.
  - Under lock contention (>200ms), self-contained envelopes (`<timestamp_ns_20d>_<pid>_<monotonic_ns>.json`) are written via atomic rename (`.tmp` &rarr; `.json`). Contention envelopes omit lock-dependent fields and write immutable `arrival_ns` and `enqueued_ns`. Spool directory is capped at 100 envelopes, **strictly protecting close events from pruning** (only oldest non-close status envelopes are unlinked). Replay close guard evaluates `envelope["arrival_ns"] >= session.get("admitted_at_ns", 0)`.
  - **Bounded Spool Drain with Poison-Pill Quarantine**: Reconciler and process drain up to 16 envelopes per pass. Corrupt envelopes are quarantined to `$STATE_DIR/spool/bad/<filename>` (bounded to max 20 files) so FIFO is never blocked.
  - **60-Second Hardened Post-Close Tombstone Table with Source Timestamps & Working-Only Admission**:
    - When a container is closed (`pane.closed`, `tab.closed`, `workspace.closed`), `tombstones[canonical_pane] = {"closed_at_ns": closed_at_ns, "closed_source_ts": closed_source_ts, "last_source_timestamp": ts}` is recorded where `closed_source_ts = max(event.data.get("timestamp", 0.0), session.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9)`.
    - Late status events arriving with `arrival_ns <= closed_at_ns` are strictly rejected.
    - Trailing status events arriving with `event.data.timestamp <= last_source_timestamp` are strictly rejected.
    - **Strict Re-Admission Filter within 60s Window**: To pop a tombstone and admit a new session within the 60s window, ALL of the following criteria must be met:
      1. Fresh turn start in `"working"` status (`agent_status == "working"`); idle, done, or unknown events NEVER pop tombstones.
      2. Source timestamp evaluation:
         - If `src_ts` is not None: `src_ts > closed_source_ts` and `src_ts > last_source_timestamp`.
         - If `src_ts` is missing / None: `arrival_ns > closed_at_ns` with positive working admission.
      3. Positive agent metadata present (`event.data.agent` non-empty) AND Herdr process alive (`is_herdr_alive()`).
    - **Container Close vs Agent Exit Tombstone Discipline with `agent_exits` Table**: Container closures record a container tombstone for 60s. Agent exit to `Ended` is an ordinary process exit within a surviving shell: it transitions to `Ended` and is evicted from cache upon confirmed delivery, but does **NOT** record a container tombstone (`close_kind == "agent_exit"`), allowing a user to relaunch an agent immediately in the same open terminal pane without tombstone delays. To prevent trailing out-of-order status events from resurrecting an exited agent session, the cache records `agent_exits: { "<canonical_pane>": { "exit_at_ns": <int>, "exit_source_ts": <float> } }`. Late events arriving with `arrival_ns <= exit_at_ns` or `src_ts <= exit_source_ts` are dropped. A fresh positive `working` event immediately clears the `agent_exits` entry and admits without delay. `agent_exits` entries are pruned after 60s.
  - **Dual Teardown Guarantee (Herdr 0.8.x Contract)**: When a container is closed, Herdr emits container events (`tab.closed`, `workspace.closed`) and individual `pane.closed` events for every enclosed pane. Container cascades provide fast batch cleanup, while per-pane events guarantee cleanup even if a background pane had `tab_id = null`.
- **Periodic Watchdog, Wall-Clock Absence Horizon, Sub-Second Wakeup & 256 Capacity Discipline**:
  - Whenever an event leaves at least one active non-Ended session (`Working`, `Waiting`, `Idle`, `Done`), it ensures the detached background watchdog is running.
  - **Sub-Second Reconciler Wakeup & `reconciler.pending` Lifecycle**: The background reconciler sleeps in short **0.5-second ticks**, checking `$STATE_DIR/reconciler.pending` and spool files on every tick. Any deferred delivery or spool arrival breaks sleep within 0.5s (satisfying the <=1.0s maximum latency criterion). Touching `reconciler.pending` resets `bartender_absent_since = None` (immediately cancelling absence backoff). `reconciler.pending` is atomically consumed: unlinked BEFORE the sweep pass and re-checked immediately after; if re-touched during the sweep, the loop re-runs without sleeping.
  - **Quiet Outage Recovery Loop**: If any session is in `retryable_exhausted` status or `DELIVERY_DOWN` exists, the reconciler probes `GET /health` on every pass. When healthy, it clears `DELIVERY_DOWN`, resets `delivery_attempts = 0`, sets `delivery_status = "in_flight"`, and reconciles immediately.
  - **Wall-Clock Bartender Absence Horizon**: Reconciler polls Bartender process status (`get_bartender_pid()`). If absent >1000s, sleep backs off to 300s. **Terminal Horizon**: If Bartender is absent for **>12 hours (43200s)** and Herdr is dead, the reconciler exports all undelivered sessions to `$HOME/.herdr-bartender-orphans.json` (mode `0600`) and cleanly exits.
  - **Active Session Capacity Protection (256 Cap)**: `active-sessions.json` is capped at 256 sessions. Eviction prunes oldest `Ended` and `salvaged` records first. **Active running sessions are NEVER evicted**; if 256 active sessions exist, admission of new session 257 is refused with a warning log. `pane_generations` is capped at 512 entries, pruning only keys not referenced by active sessions, tombstones, OR orphans.
  - **Safe Lock Discipline (<10ms CPU-Bounded Step C)**: Orphan file operations (`export_orphan_record`, `remove_orphan_record`) and process spawns (`ensure_reconciler_running`) are staged under cache lock and executed strictly OUTSIDE the lock.
  - **Log Rotation**: `log_debug` automatically rotates `$STATE_DIR/plugin.log` to `plugin.log.1` when exceeding 1MB (maximum 2MB disk footprint).
  - Bounded TTLs: `Working` (12h), `Waiting` (48h), `Idle`/`Done` (24h), Herdr dead >5m. Sessions undelivered for >12h past TTL are exported to orphans and evicted.
- **Bridge Outage Recovery, Restart Detection & Selective Quiescent Salvage**:
  - Canonical status enum: `"delivered"`, `"in_flight"`, `"retryable_exhausted"`, `"non_retryable_failed"`, `"salvaged"`.
  - Bridge Restart / Re-Sync: Detected when `get_bartender_pid()` detects a new PID and verifies the previous PID is dead (`os.kill(pid, 0)` raises `OSError`), or after bridge reconnection following `DELIVERY_DOWN`. Triggers Full Top Shelf Re-Sync (`delivered_seq = 0`).
  - **Selective Spool Preservation & Quiescent Salvage**: On corrupt cache salvage:
    - **Close Event Preservation**: Spool envelopes are inspected before quarantine: Close events (`pane.closed`, `tab.closed`, `workspace.closed`, or `state == "Ended"`) are **strictly preserved in `spool/`** to be replayed, ensuring closed panes are not stranded on Top Shelf. Only non-close status envelopes are moved to `spool/bad/` (capped at 20 files).
    - **Quiescent Salvage & 5-Minute Bounded Horizon**: Candidate session IDs matching `^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$` are staged quiescently as `desired_state = "Idle"`, `delivered_state = "Idle"`, `seq = 1, delivered_seq = 1`, `delivery_status = "salvaged"`, `salvaged = True` under epoch-dominating generation `salvage_epoch_gen = max(int(time.time()), 1_700_000_000)`. All existing pane markers are removed on salvage so vendor hooks immediately fall through. Salvaged sessions are **strictly excluded from marker heartbeats** and expire to `Ended` after a deterministic 5-minute (300s) quiescent horizon (or immediately if Herdr is dead, `now_wall - last_ts > 300 or not is_herdr_alive()`), rather than lingering for a 24h TTL, until a fresh Herdr event delivers confirmed state.
- **Zero-Data-Loss Ended Retention, Cleanup Exit Code Contract & Protected Orphan Replay**:
  - Bartender returns HTTP 200 for absent sessions; HTTP 4xx indicates payload rejection.
  - On Ended failure or rejection, the payload is automatically retried once with minimal fields `{"state": "Ended", "agent": "Herdr", "session_id": sid}`.
  - **Unified Ended Lifecycle**: Unconfirmed Ended sessions are retained in `active-sessions.json` during active retry passes and immediately mirrored to `$HOME/.herdr-bartender-orphans.json` (mode `0600`).
  - **Automatic Orphan Replay**: Whenever the background reconciler finds `$HOME/.herdr-bartender-orphans.json` present and bridge `/health` succeeds, it automatically replays and clears orphaned sessions under an exclusive `.herdr-bartender-orphans.json.lock`.
  - **Single Normative Orphan Replay Guard & Post-Send Re-Sync**: As defined normatively in §9.2:
    - An orphan record for `session_id` is skipped iff: `active_s = cache.sessions.get(session_id)` (or matching `pane_id`) satisfies `active_s is not None and not active_s.get("salvaged", False) and active_s.get("desired_state") != "Ended"`.
    - Salvaged sessions (`salvaged == True`) and epoch-dominating salvage generations (`>= 1_700_000_000`) are **strictly excluded from suppressing orphan replays**, guaranteeing pre-crash Ended records are always transmitted to Bartender to clear Top Shelf.
    - Post-Send Re-Sync: Immediately after orphan `Ended` transmission, the reconciler re-verifies the cache lock; if a fresh active session was admitted while `Ended` was in flight, it forces a re-sync (`delivered_seq = 0, delivery_status = "in_flight"`) and touches `reconciler.pending`.
  - `--cleanup` budget scales dynamically: `timeout = max(10.0, len(sessions) * 0.15)`.
  - **Cleanup Exit Code Contract**:
    - Exit `0`: All sessions confirmed Ended on bridge (HTTP 200).
    - Exit `2`: Bridge unreachable or sessions rejected; pending sessions exported to canonical `$HOME/.herdr-bartender-orphans.json` (outside state directory, mode `0600`).
    - Exit `1`: Fatal error during cleanup.
  - **Installation, Status & Uninstallation Contract**: `--install-hooks` and `--uninstall-hooks` provide deterministic, mode-preserving hook patching and restoration (`orig_mode | 0o100`), verified with `bash -n` and atomic `os.replace`. Auto-repair detects upstream modifications via SHA-256 against `vendor-hook-sha.json`; on mismatch, it touches `$STATE_DIR/HOOK_NEEDS_REVIEW` rather than silently overwriting. `herdr-bartender --status` inspects and reports `[WARNING] Vendor hook modified upstream (SHA mismatch). Run 'herdr-bartender --install-hooks' to re-verify and approve changes.` Running `--install-hooks` approves changes and removes `$STATE_DIR/HOOK_NEEDS_REVIEW`.
- **Single Herdr Instance Precondition & Local Process Liveness Gate**:
  - **Single Herdr Instance Precondition**: Enforced by macOS GUI application architecture under launchd bundle identifier `com.herdr.app` and single domain socket. If `pgrep -xi herdr` yields multiple PIDs, `get_herdr_pid()` inspects process start times (`ps -p <pid> -o lstart=`) and selects the instance with the **earliest process start time**, deterministically identifying the primary GUI application and remaining immune to PID wraparound and transient CLI tools. A change in Herdr PID triggers mass session expiry only if the previous PID is confirmed dead (`os.kill(last_herdr_pid, 0)` raises `OSError`).
  - **Bartender Process Identity**: `get_bartender_pid()` queries `pgrep -x "Bartender 6" || pgrep -x "Bartender"` and selects the process with earliest start time via `ps -p <pid> -o lstart=`, matching Herdr's selection logic. Full Re-Sync triggers only when the previous Bartender PID is confirmed dead (`os.kill(last_bartender_pid, 0)` raises `OSError`).
  - Before transmitting any HTTP request to the loopback listener `http://127.0.0.1:${NOTCHBAR_AGENTS_PORT:-7823}`, the plugin verifies peer liveness via `get_bartender_pid()`. Under the macOS single-user desktop security model, this defends against accidental sends to unrelated local listeners when Bartender is closed. Remote SSH forwarding is explicitly declared OUT OF SCOPE for v1.0.

---

## 2. Normative Event & State Mapping

### 2.1 State & Lifecycle Transition Matrix

| Herdr Event | Herdr `agent_status` | Bartender `state` | Top Shelf Visual | Session Action | Qualification & Edge Case Rules |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `pane.agent_status_changed` | `"working"` | `"Working"` | Active spinner | Upsert session | **Qualified panes only**: Increments `seq`, updates `desired_state = "Working"`, sets `delivery_status = "in_flight"`. TTL: 12 hours. |
| `pane.agent_status_changed` | `"blocked"` | `"Waiting"` | Highlighted attention banner | Upsert session | **Qualified panes only**: Increments `seq`, updates `desired_state = "Waiting"`, sets `delivery_status = "in_flight"`. Surfaces user attention prompt in NotchBar. TTL: 48 hours. |
| `pane.agent_status_changed` | `"done"` | `"Done"` | Green checkmark / success | Upsert session | **Qualified panes only**: Increments `seq`, updates `desired_state = "Done"`, sets `delivery_status = "in_flight"`. TTL: 24 hours. |
| `pane.agent_status_changed` | `"idle"` | `"Idle"` | Resting dot | Update if active | **Qualified panes only**: If agent is active at interactive prompt, increments `seq`, updates to `Idle`. If agent exited shell (see 2.2), transitions to `Ended`. TTL: 24 hours. |
| `pane.agent_status_changed` | `"unknown"` | *(none)* | *(retain last state)* | Debounced / Retained | **Anti-Flap Rule**: Herdr may report `unknown` transiently during agent reattach or startup. Do not evict; retain existing session state until `pane.closed` is received. |
| `pane.agent_status_changed` | *(unrecognized)* | *(none)* | *(no change)* | Ignore | Log warning to `plugin.log`; do not mutate cache. |
| `pane.closed` | *(any / N/A)* | `"Ended"` | Removed from bar | Evict matching pane session | Increments `seq`, sets `desired_state = "Ended"`. Follows lease protocol. Evicted upon delivery confirmation. Cleans up hex pane marker; dismisses UUID-bearing `.vendor_active` outside lock and unlinks bare-touch `.vendor_active` files upon pane closure (`is_pane_closed=True`). Exempt from arrival drops if monotonic causality check passes. |
| `tab.closed` | *(any / N/A)* | `"Ended"` | Removed from bar | Evict matching tab sessions | For every session where cached `tab_id` matches closed tab: increments `seq`, sets `desired_state = "Ended"`. Follows lease protocol. Validated against `^[a-zA-Z0-9_:-]{1,48}$`. Dual Teardown Note: Herdr 0.8.x emits `tab.closed` as an opportunistic fast batch sweep AND subsequent per-pane `pane.closed` events for every child pane, ensuring panes with `tab_id = null` are still closed authoritatively. |
| `workspace.closed` | *(any / N/A)* | `"Ended"` | Removed from bar | Evict matching workspace sessions | For every session where cached `workspace_id` matches closed workspace: increments `seq`, sets `desired_state = "Ended"`. Follows lease protocol. Validated against `^[a-zA-Z0-9_:-]{1,48}$`. Dual Teardown Note: Herdr 0.8.x emits `workspace.closed` and per-pane `pane.closed` events for every child pane. |

### 2.2 Agent Session Qualification & Exit Detection

1. **Admission**: A pane is admitted as a tracked agent session **if and only if**:
   - `canonical_pane_id` strictly matches `^[a-zA-Z0-9_:-]{1,48}$` (invalid IDs rejected and logged), AND
   - One of the following holds:
     - `event.data.agent` is non-empty (after stripping whitespace), OR
     - `context.focused_pane_agent` is non-empty **AND** `context.focused_pane_id == canonical_pane_id`, OR
     - An active session record for this `session_id` already exists in `active-sessions.json` **AND** its current `desired_state != "Ended"`.
   - Upon admission (or restart of an agent turn over an `Ended` session), records `admitted_at_ns = time.time_ns()` to establish the session generation.
2. **Unambiguous Agent Status & Exit Rules for `idle`**:
   - **Case A (Active Agent at Interactive Prompt)**: If `agent_status == "idle"` AND `event.data.agent` is present and non-empty (e.g. `"claude"`):
     - Agent turn completed; process is idling at interactive shell prompt.
     - Session updates to `desired_state = "Idle"`, increments `seq`.
   - **Case B (Agent Process Exit inside Surviving Shell)**: If `agent_status == "idle"` AND (`event.data.agent` is explicitly `null` OR `""`):
     - The agent CLI process terminated, returning the pane to bare interactive shell.
     - Session transitions to `desired_state = "Ended"`, increments `seq`, stages delivery to clear Top Shelf and delete pane marker.
   - **Case C (Omitted Agent Key)**: If `event.data` omits the `agent` key entirely:
     - If an active session exists with cached `raw_agent` and `desired_state != "Ended"`: retain existing agent metadata and transition to `Idle`.
     - Otherwise: treat as unassisted bare shell and discard.
3. **Normative State Directory Resolution**:
   Both Python (`get_state_dir()`) and Bash (`HOOK_GUARD_TEMPLATE`, rollback, and maintenance scripts) resolve the plugin state directory identically according to the single normative rule:
   `${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}`.
   If `HERDR_PLUGIN_STATE_DIR` is set in the environment, its path is used directly. Otherwise, if `XDG_STATE_HOME` is set, `$XDG_STATE_HOME/herdr/plugins/herdr-bartender` is used. Otherwise, it defaults to `$HOME/.local/state/herdr/plugins/herdr-bartender`.

### 2.3 Strict Context Isolation & Resolution Hierarchy

All context lookups are strictly conditioned on pane focus (`is_focused = bool(focused_id and focused_id in (canonical_pane, raw_pane))`). If `focused_pane_id` is missing, `None`, or empty, `is_focused` evaluates strictly to `False` to guarantee that background pane events NEVER inherit focused tab, workspace, or cwd attributes:

| Bartender Field | Authoritative Source Hierarchy | Sanitization Whitelist & Mismatch Rules |
| :--- | :--- | :--- |
| `session_id` | `herdr:{sanitized_host}:{canonical_pane_id}` | `canonical_pane_id` strictly validated against `^[a-zA-Z0-9_:-]{1,48}$`. Invalid or over-length IDs are rejected and logged (never truncated). `sanitized_host` is `re.sub(r'[^a-z0-9_-]', '', raw_host.lower())[:32]` (fallback `"local"`). Total length bounded to **96 chars** (`^[a-zA-Z0-9_:-]{1,96}$`). |
| `agent` | 1. `event.data.agent`<br>2. `context.focused_pane_agent` (**ONLY if focused**)<br>3. Existing cached `raw_agent`<br>4. `"Herdr"` | Normalized through `AGENT_NAME_OVERRIDES`. Stripped of ANSI, OSC, and control characters; truncated to 64 chars. Never uses focused agent for background panes. |
| `title` | 1. `event.data.title`<br>2. Existing cached `title`<br>3. `context.workspace_label` (**ONLY if focused**)<br>4. `"Pane {canonical_pane_id}"` | Stripped of all ANSI CSI, OSC, DCS, and control characters; truncated to 120 characters. |
| `cwd` | 1. `event.data.cwd`<br>2. Existing cached `cwd`<br>3. `context.focused_pane_cwd` (**ONLY if focused**)<br>4. `context.workspace_cwd` (**ONLY if focused**)<br>5. `""` | Stripped of control characters; truncated to 256 characters. Background panes default to `""` if not provided in `event.data`. |
| `tab_id` | 1. `event.data.tab_id`<br>2. Existing cached `tab_id`<br>3. `context.tab_id` (**ONLY if focused**)<br>4. `None` | Validated against `^[a-zA-Z0-9_:-]{1,48}$`. Null if invalid or unresolved. Background panes never inherit focused tab. Herdr 0.8.x container close guarantees companion `pane.closed` for every child pane, guaranteeing cleanup even if `tab_id` is null. |
| `workspace_id` | 1. `event.data.workspace_id`<br>2. Prefix before `:` in `canonical_pane_id`<br>3. Existing cached `workspace_id`<br>4. `context.workspace_id` (**ONLY if focused**)<br>5. `""` | Validated against `^[a-zA-Z0-9_:-]{1,48}$`. Derived strictly via `normalize_pane_id(pane_id, workspace_id)` in Python and equivalent bash logic: if `raw_pane_id` contains `:`, it is used directly as `canonical_pane` without workspace prefixing or override. If `raw_pane_id` lacks `:`, it requires `workspace_id` or `HERDR_WORKSPACE_ID` to be non-empty; if missing or empty, it derives `""` and the event is dropped / fails open, never falling back to `"default"`. In hook guards and marker operations, canonical pane hex encoding is strictly enforced without un-prefixed raw fallbacks, preventing cross-workspace marker collisions. |
| `terminal` | Constant `"Herdr"` | Always `"Herdr"`. |
| `event` | Source event name | Sanitized alphanumeric/period string (max 64 chars). |
| `seq` | Current `transmitting_seq` | Included in wire payload for diagnostic tracing and operator audit. |

---

## 3. Bartender Pro HTTP Bridge Empirical Contract

### 3.1 Live Verified Bridge Facts Table (Bartender Pro 6.0.4 on macOS)

The HTTP bridge contract was empirically validated against live Bartender Pro 6 on macOS (PID 982):

| Contract Item | Empirical Verification Result | Live Test Command & Observed Output |
| :--- | :--- | :--- |
| **Process Name** | `Bartender 6` | `pgrep -x "Bartender 6"` returned PID `982`. (`pgrep -x "Bartender"` returns nothing on macOS). |
| **Default Port** | `7823` | `curl -s http://127.0.0.1:7823/health` returned `{"sessions":0,"port":7823,"ok":true}`. |
| **Supported States** | `Working`, `Waiting`, `Done`, `Idle` | `POST /event` with `{"state":"Working|Waiting|Done|Idle", ...}` returned `{"ok":true}` for each state. |
| **Tolerance of Extra Fields** | Accepted and ignored | `curl -X POST http://127.0.0.1:7823/event -d '{"state":"Working","agent":"Claude","session_id":"t1","terminal":"Herdr","seq":1}'` returned `{"ok":true}`. |
| **Long Session IDs** | Supports at least 106 chars | `POST /event` with 106-character `session_id` returned `{"ok":true}` without truncation. |
| **Eviction via `Ended`** | Decrements session count | Posting `state: "Ended"` for `t1` caused `/health` session count to return to `0`. |
| **Unknown Session Eviction** | Idempotent HTTP 200 OK | Posting `state: "Ended"` for an unknown/non-existent `session_id` returned `{"ok":true}`. |
| **Malformed JSON** | Returns HTTP 400 Client Error | Posting invalid JSON returned `HTTP 400` with `{"error":"invalid JSON"}`. |
| **Restart Detection** | PID tracking | Monitored via `get_bartender_pid()` searching `["Bartender 6", "Bartender"]`, selecting process with earliest start time (`ps -p <pid> -o lstart=`). Restart requires previous PID confirmed dead (`os.kill(last_pid, 0)` raises `OSError`). |
| **Vendor Hook Contract & Session Eviction** | Native vendor hooks post UUID-keyed sessions | Claude posts `POST /event` with `Working|Waiting|Done|Idle|Ended`. Codex posts turn events only (`Working|Waiting|Done`). Verified: posting `state:"Ended"` with the vendor UUID decrements `/health` `sessions` count to 0, validating `cleanup_vendor_active` dismissal. |

### 3.2 Verified Vendor Hook Interface Facts Table (Claude Code & OpenAI Codex CLI)

Empirical testing and interface analysis of native vendor hooks on macOS establishes the following facts:

| Vendor CLI & Hook Script | Events Emitted (argv & stdin JSON) | Emits SessionEnd / Ended? | Native Top Shelf Dismissal Mechanics & Fallback Dismissal Paths |
| :--- | :--- | :--- | :--- |
| **Claude Code**<br>`claude-event-hook.sh` | `SessionStart`, `UserPromptSubmit`, `PreToolRun`, `PostToolRun`, `SessionEnd` | **YES** (`SessionEnd` maps to `Ended`) | When Claude Code exits cleanly, it emits `SessionEnd`. The native hook posts `state: "Ended"` for its UUID to Bartender, evicting the Top Shelf entry. The hook guard lets `SessionEnd`/`Ended` pass through unconditionally. |
| **OpenAI Codex CLI**<br>`codex-notify-hook.sh` | `AgentWaiting`, `AgentDone` (turn-terminal only) | **NO** (never emits `SessionEnd` or `Ended`) | Codex CLI lifecycle hooks are turn-oriented and **do not emit session-terminal events**. Because Codex never emits `SessionEnd`, dismissal of native Codex entries cannot rely on vendor hook pass-through. Instead, dismissal is authoritatively driven by Herdr: <br>1. **Herdr Agent Exit**: When Codex exits the interactive shell, Herdr detects agent termination and emits `pane.agent_status_changed` with `agent = null` / `""`. Herdr transitions the session to `Ended`, which triggers `cleanup_vendor_active` to send `Ended` for the vendor UUID.<br>2. **Herdr Container Closure**: When the pane, tab, or workspace is closed, `handle_pane_closed` calls `cleanup_vendor_active(pane, is_pane_closed=True)` to send `Ended` for the vendor UUID.<br>3. **No Blind Sweep During Outages**: While Herdr is dead, `.vendor_active` is NEVER swept by the reconciler based on mtime, preserving native vendor fallback indefinitely until Herdr recovers, pane closes, or vendor session-terminal event unlinks it. |

### 3.3 Authoritative Lifecycle, Retry & Eviction State Machine

| HTTP Status / Condition | Response Body / Error | Classification | Cache Action | Reconciler Retry Schedule | Terminal Eviction Policy |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `200` | `{"ok":true, ...}` | **Success** | Set `delivered_state = transmitting_state`, `delivered_seq = transmitting_seq`. Reset `delivery_attempts = 0`. Reset `consecutive_failures = 0`. Clear `delivery_error = null`. Set `delivery_status = "delivered"`. Clear `DELIVERY_DOWN` and `<hex>.failed`. Touch pane marker to refresh mtime. Re-arm any `"retryable_exhausted"` sessions to `"in_flight"`. Stage vendor dismissal outside lock. | None needed. | If `transmitting_state == "Ended"` AND `seq == transmitting_seq`: evict session from cache, delete pane marker, and stage vendor cleanup outside lock. `cleanup_vendor_active` unlinks `.vendor_active` (including bare touches) upon confirmed delivery and on pane close to restore deduplication; if `.vendor_active` contains an extracted UUID, sends `Ended` to bridge outside lock. |
| `200` | `{"ok":false, ...}` | **Bridge Rejection** | Log warning. Mark `delivery_status = "non_retryable_failed"`, `delivery_error = "bridge_rejected"`, `rejected_seq = transmitting_seq`. Do NOT increment `delivered_seq`. Touch `<hex>.failed` and remove marker so vendor hooks fall through immediately. | None (non-retryable). Excluded from retry loop until new event advances `seq`. | If `transmitting_state == "Ended"`: retry once with minimal payload `{"state":"Ended","agent":"Herdr","session_id":sid}`. If still rejected, retain in cache during retries, mark `orphaned_ended = true`, and export to canonical `$HOME/.herdr-bartender-orphans.json` (mode `0600`). Evicted from cache only after 12h absence horizon or under 256-cap pruning. |
| `3xx` | *(any)* | **Protocol Anomaly** | Unexpected redirect. Mark `delivery_status = "non_retryable_failed"`, `delivery_error = "unexpected_redirect"`, `rejected_seq = transmitting_seq`. Touch `<hex>.failed` and remove marker. | None (non-retryable). Excluded from retry loop until new event advances `seq`. | If `transmitting_state == "Ended"`: retry once with minimal payload. If still rejected, mark `orphaned_ended = true`, retain in cache, export to orphan file, and evict only upon 12h horizon or 256 cap. |
| `400`–`499` | *(any)* | **Client Error** | Malformed payload or rejection (Bartender returns 200 for absent sessions; 4xx indicates schema/payload rejection). Mark `delivery_status = "non_retryable_failed"`, `delivery_error = "4xx_client_error"`, `rejected_seq = transmitting_seq`. Touch `<hex>.failed` and remove marker. | None (non-retryable). Excluded from retry loop until new event advances `seq`. | If `transmitting_state == "Ended"`: automatically retry once with minimal payload `{"state":"Ended","agent":"Herdr","session_id":sid}`. If still rejected, do NOT evict from cache immediately! Mark `orphaned_ended = true`, retain in cache, and mirror to `$HOME/.herdr-bartender-orphans.json` (mode `0600`). Evicted from cache only upon 12h horizon or 256 cap. |
| `500`–`599` | *(any)* | **Server Error** | Bridge fault. Increment `delivery_attempts += 1`. Set `delivery_error = "5xx_server_error"`. Retain `delivered_seq < seq`. Touch `<hex>.failed` immediately on first failure. Increment `consecutive_failures += 1`; if >= 3, touch `DELIVERY_DOWN`. | Background helper retries up to 5 attempts (0s, 1s, 2s, 4s, 8s). Releases lease before each sleep; re-claims under lock on waking. | If 5 attempts fail: mark `delivery_status = "retryable_exhausted"`, remove marker. If `desired_state == "Ended"`, mark `orphaned_ended = true`, retain in cache, mirror to orphan file. Reconciler active health probe re-arms on recovery. Evicted from cache only upon 12h horizon or 256 cap. |
| Network Fail | Connection refused, timeout | **Network Error** | Bridge unreachable. Increment `delivery_attempts += 1`. Set `delivery_error = "network_timeout"`. Retain `delivered_seq < seq`. Touch `<hex>.failed` immediately on first failure. Increment `consecutive_failures += 1`; if >= 3, touch `DELIVERY_DOWN`. | Background helper retries up to 5 attempts (0s, 1s, 2s, 4s, 8s). Releases lease before each sleep; re-claims under lock on waking. | If 5 attempts fail: mark `delivery_status = "retryable_exhausted"`, remove marker. If `desired_state == "Ended"`, mark `orphaned_ended = true`, retain in cache, mirror to orphan file. Reconciler active health probe re-arms on recovery. Evicted from cache only upon 12h horizon or 256 cap. |
| Stale TTL | Internal Sweep | **TTL Expiry** | Applies to `Working` (>12h), `Idle`/`Done` (>24h), `Waiting` (>48h), or Herdr dead >5m. | Background helper stages `desired_state = "Ended"`, `seq = seq + 1`. Permitted for all sessions including salvaged records. | Transmits `Ended` to bridge. ONLY upon confirmed 200 OK delivery is the session evicted and marker deleted. If undelivered >12h past TTL, exported to orphan file and evicted from cache. |

### 3.4 Live Verified Facts Table (Herdr 0.8.x on macOS)

The plugin execution and runtime contract was empirically captured and verified against Herdr 0.8.x running on macOS:

| Contract Item | Empirical Verification & Captured Fact | Operational Policy & Degraded Behavior Fallback |
| :--- | :--- | :--- |
| **Plugin Executable Invocation** | Invoked as `./bin/herdr-bartender <event_name>` with complete JSON envelope streamed to stdin. Captured raw envelope: `{"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude", "agent_status": "working", "timestamp": 1728000000.123}, "context": {"focused_pane_id": "w1:p1", "focused_pane_agent": "claude", "focused_pane_cwd": "/workspace", "workspace_id": "w1", "workspace_label": "Dev", "tab_id": "w1:t1"}}`. | Primary parser reads stdin JSON. If stdin is empty or malformed, fallback inspects argv `${1:-}` to execute safe no-op or background recovery. |
| **Herdr Process Model** | Running GUI app registers under bundle `com.herdr.app` with process name `herdr`. Helper CLI commands (`herdr plugin list`) run transiently with the same name. | `get_herdr_pid()` prioritizes GUI `.app` bundles (`ps -p <pid> -o comm=`) over CLI helper commands, then selects the PID with earliest start time. Converts start time to an integer epoch string (`time.mktime(...)` from `lstart`), eliminating colons and whitespace. Tracks `last_herdr_pid` and `last_herdr_start_time` in cache to detect restarts across both PID changes and start time changes on the same PID (PID reuse defense). |
| **Execution Deadline & Timeout** | Herdr plugin supervisor enforces a hard 2.0s SIGKILL limit on synchronous plugin executions. | Plugin arms internal `signal.ITIMER_REAL` for 1.5s (500ms safety buffer). All operations draw from dynamic budget `time_remaining()`. At 1.5s, the SIGALRM signal handler sets `PENDING_WATCHDOG_EXIT = True` (zero file I/O in signal handler context). If outside critical sections, it performs non-blocking helper spawn and exits 0 cleanly. If inside a critical section, exit is deferred until the atomic swap and lock release complete, guaranteeing zero file corruption. |
| **Terminal Pane Environment Variables & Colon-less Pane Rejection** | In terminal shells, Herdr exports `HERDR_PANE_ID` (`w1:p1` or `p1`) and `HERDR_WORKSPACE_ID` (`w1`). Both match `^[a-zA-Z0-9_:-]{1,48}$`. Verified Herdr fact: colon-less pane IDs without workspace_id in event data are invalid and rejected to prevent `default:p1` vs `w1:p1` divergence. | Single canonical pane derivation parity: both Bash and Python derive the canonical pane ID 1:1 identically as `${ws}:${raw_pane}` without secondary alias markers, symlinks, or dual-linking. If `raw_pane` lacks `:` and neither `workspace_id` nor `HERDR_WORKSPACE_ID` is available, the event is logged and rejected. |
| **Dual Teardown Guarantee** | When a tab or workspace is closed, Herdr emits container events (`tab.closed`, `workspace.closed`) AND subsequent per-pane `pane.closed` events for every enclosed pane. | Container cascade handler evicts all matching sessions in cache; container cascade sends at most 1 session inline (<200ms) and delegates remainder to reconciler. If container close is missed or per-pane close is delayed, either path authoritatively closes the session. |
| **Timestamp Presence & Ordering** | Status events carry floating-point epoch `timestamp` in `event.data`. | Preserved in cache as `last_source_timestamp`. If `timestamp` is missing or null, `src_ts = None`; container tombstone re-admission permits positive working admission when `arrival_ns > closed_at_ns` with valid agent metadata. |
| **Agent Exit Signal** | When agent process terminates in interactive shell, Herdr emits `pane.agent_status_changed` with `agent_status = "idle"` and `agent = null` (or `""`). | Evaluated under Rule 2.2 Case B: transitions session to `Ended`, advancing `seq` and evicting Top Shelf entry upon delivery confirmation. Agent process exit does NOT create a container tombstone (the terminal pane remains open). Only container close events (`pane.closed`, `tab.closed`, `workspace.closed`) record container tombstones. |
| **Loopback Bridge Port** | Configured via `NOTCHBAR_AGENTS_PORT` (integer 1024–65535, default `7823`). | Validated integer. If unset or invalid, defaults strictly to `7823`. |

---

## 4. Concurrency, Serialization & Client-Side Delivery Guarantees

### 4.1 Monotonic Counters, Timestamps & Tombstones
- Each session in `active-sessions.json` maintains:
  - `seq`: Strictly incremented per-session monotonic integer counter tracking state advancements.
  - `generation`: Monotonic integer generation counter incremented whenever an agent starts a new session or transitions from `Ended`. Spooled events are evaluated by causal timestamp ordering: an event is dropped if its arrival timestamp is older than the current session's `admitted_at_ns` (`envelope['arrival_ns'] < session.get('admitted_at_ns', 0)`).
  - `delivered_seq`: Integer recording the last `seq` confirmed delivered by HTTP 200 OK (`{"ok":true}`).
  - `rejected_seq`: Integer recording the last `seq` rejected with non-retryable error.
  - `admitted_at_ns`: Integer nanosecond wall-clock epoch timestamp (`time.time_ns()`) of session admission.
  - `last_event_ns`: Integer nanosecond wall-clock epoch timestamp (`time.time_ns()`) of last applied mutation, clamped against backward clock jumps via `last_event_ns = max(now_ns, session.get("last_event_ns", 0) + 1)`.
  - `closed_at_ns`: Integer nanosecond wall-clock epoch timestamp (`time.time_ns()`) recorded once when close is admitted. Preserved as a stable origin across retry and tombstone transitions.
  - `lease_token`: Unique string token in format `f"{pid}:{PROCESS_START_TIME}:{now}:{session_id}"`.
  - `lease_deadline`: Epoch timestamp (`now + 1.5s`) after which a hung process lease may be taken over.
- **Root-Level `next_generation` & `pane_generations` Invariant**:
  - `active-sessions.json` maintains `next_generation: int` (initialized to 1) and `pane_generations: { "<canonical_pane>": <int> }` at the root.
  - Evicting a session from `sessions` upon confirmed `Ended` does NOT reset its generation counter or decrement `next_generation`.
  - When an agent turn starts or re-admits a pane, it computes: `curr_gen = max(data.get("next_generation", 1), pane_generations.get(canonical_pane, 0), session.get("generation", 0)) + 1`. It stores `curr_gen` in `next_generation`, `pane_generations[canonical_pane]`, and `session["generation"]`. This guarantees global and per-pane monotonicity across all evictions, restarts, and pruning.
- **Root-Level Cache Monotonicity & Hostname Pinning**:
  - `cache_seq`: Strictly incremented monotonic integer counter at the root of `active-sessions.json`, advanced on every `save()` operation.
  - `host`: Sanitized hostname pinned once at cache creation in `active-sessions.json` root. All session IDs read `host = data.get("host") or get_sanitized_hostname()`, guaranteeing stable session IDs even if macOS network changes alter the local hostname mid-session.
- **Top-Level Arrival Timestamp**:
  - `PROCESS_ARRIVAL_TIME_NS = time.time_ns()` is captured immediately upon process invocation before any lock wait, and passed to all event handlers as `arrival_ns`.
- **60-Second Post-Close Tombstone Table with Source Timestamps & Positive Working Admission**:
  - `active-sessions.json` maintains `tombstones: { "<canonical_pane>": { "closed_at_ns": <int>, "closed_source_ts": <float>, "last_source_timestamp": <float> } }`.
  - When a pane is closed, `closed_source_ts = max(float(event_data.get("timestamp") or 0.0), last_source_timestamp, float(arr_ns) / 1e9)` is recorded under lock along with `closed_at_ns` and `last_source_timestamp`, and retained for 60 seconds (pruned safely when `now_ns - closed_at_ns > 60s`). Preserving the initial `closed_at_ns` origin prevents delayed close confirmation from artificially moving the closure horizon forward.
  - Late status events arriving with `arrival_ns <= closed_at_ns` are strictly rejected.
  - Trailing status events arriving with `event.data.timestamp <= last_source_timestamp` are strictly rejected, preventing delayed status updates from resurrecting closed panes.
  - Within the 60-second tombstone window (`arr_ns > closed_at_ns` and `arr_ns - closed_at_ns < 60s`), popping the tombstone requires ALL of the following **positive working admission signals**:
    1. `event.data.agent_status == "working"` (idle, waiting, blocked, or done states cannot resurrect a recently closed pane).
    2. Source timestamp evaluation:
       - If `src_ts` is not None: `src_ts > closed_source_ts and src_ts > last_source_timestamp`.
       - If `src_ts` is missing / None: `arr_ns > closed_at_ns` with positive working admission.
    3. `event.data.agent` must be explicitly present and non-empty.
    4. Herdr process must be actively running (`pgrep -xi "herdr"`).
    - If ANY condition is absent (e.g. unassisted shell, missing agent, non-working state, or pre-close timestamp), the event is dropped and the tombstone is preserved.
    - If ALL positive admission conditions are met, the tombstone is popped and the new session generation admitted.
- **Strict 256 Active Session Capacity**:
  - `sessions` in `active-sessions.json` is strictly capped at 256 entries.
  - Capacity pruning targets ONLY `Ended` sessions and `salvaged` records. Active live sessions (`Working`, `Waiting`, `Idle`, `Done` with `not salvaged`) are NEVER evicted.
  - If 256 active live sessions already exist, admission of session 257 is rejected with an error log to preserve state integrity.
- **Stale Send Compensation Invariant with Under-Lock Re-Verification**:
  - If a sender delivers a non-Ended event in Step B, but discovers upon re-acquiring the lock in Step C that the session was evicted (`session is None`) or that `canonical_pane in tombstones` (concurrent closure):
    - **Under-Lock Re-Verification**: The sender re-verifies under lock whether a live session was re-admitted (`active_s = data["sessions"].get(session_id)`); if `active_s and active_s["desired_state"] != "Ended"`, the compensating send is aborted.
    - Otherwise, the sender issues a compensating HTTP `POST /event` with `state: "Ended"` outside the lock to dismiss the phantom session from Top Shelf.
    - **Post-Compensation Re-Sync Detection**: After issuing the compensating send, the sender re-acquires the lock to detect races; if a new active session was admitted while compensation was in flight, it forces `active_s["delivered_seq"] = 0`, `active_s["delivery_status"] = "in_flight"`, touches `$STATE_DIR/reconciler.pending`, and ensures the background reconciler is running.
- **Source Staleness Filter**:
  - Evaluated when `event.data.timestamp` is present in the Herdr event.
  - If `event.data.timestamp < session.get("last_source_timestamp", 0) - 0.1s`: drop event as stale before incrementing `seq`.
  - True Container Closes (`pane.closed`, `tab.closed`, `workspace.closed`) are exempt from source staleness filtering and always advance `seq`.
    - **Agent Exit Ordering with `agent_exits` Table**: Agent exit to `Ended` is an ordinary status transition within a surviving shell, NOT a container teardown; agent exits are **strictly subject to arrival and timestamp ordering** (never exempt). To prevent out-of-order delayed status events from resurrecting an exited agent session, the cache records `agent_exits: { "<canonical_pane>": { "exit_at_ns": <int>, "exit_source_ts": <float> } }`. Any trailing status event arriving with `arrival_ns <= exit_at_ns` or `src_ts <= exit_source_ts` is dropped. A fresh positive `working` event immediately clears the `agent_exits` entry and is admitted without delay. `agent_exits` entries are pruned after 60 seconds.

### 4.2 Cache Schema (`active-sessions.json`)

```json
{
  "version": 4,
  "host": "macbook",
  "cache_seq": 104,
  "herdr_instance_id": "12480:1727998400",
  "last_herdr_pid": 12480,
  "last_bartender_pid": 982,
  "last_updated": 1727998410.12,
  "consecutive_failures": 0,
  "last_successful_delivery": 1727998410.12,
  "next_generation": 1,
  "pane_generations": {
    "w1:p1": 1
  },
  "tombstones": {
    "w1:pClosed": {
      "closed_at_ns": 1727998400123456789,
      "closed_source_ts": 1727998400.12,
      "last_source_timestamp": 1727998399.80
    }
  },
  "agent_exits": {
    "w1:pExit": {
      "exit_at_ns": 1727998400123456789,
      "exit_source_ts": 1727998400.12
    }
  },
  "dismissed_vendor_uuids": {
    "12345-vendor-uuid-67890": {
      "timestamp": 1727998410.12,
      "pane_hex": "64656661756c743a7031"
    }
  },
  "sessions": {
    "herdr:macbook:w1:p1": {
      "pane_id": "w1:p1",
      "workspace_id": "w1",
      "tab_id": "w1:t1",
      "host": "macbook",
      "agent": "Claude (Herdr)",
      "raw_agent": "claude",
      "title": "Fix authentication bug",
      "cwd": "/Users/voodootikigod/Projects/app",
      "desired_state": "Waiting",
      "delivered_state": "Working",
      "desired_payload": {
        "state": "Waiting",
        "agent": "Claude (Herdr)",
        "session_id": "herdr:macbook:w1:p1",
        "title": "Fix authentication bug",
        "cwd": "/Users/voodootikigod/Projects/app",
        "terminal": "Herdr",
        "event": "pane.agent_status_changed",
        "seq": 4
      },
      "seq": 4,
      "delivered_seq": 3,
      "rejected_seq": 0,
      "generation": 1,
      "admitted_at_ns": 1727998400123456789,
      "last_event_ns": 1727998410123456789,
      "last_arrival_ns": 1727998410123456789,
      "lease_token": "12844:1727998399.50:1727998410.12:herdr:macbook:w1:p1",
      "lease_deadline": 1727998411.62,
      "sending_pid": 12844,
      "delivery_attempts": 0,
      "delivery_error": null,
      "delivery_status": "in_flight",
      "orphaned_ended": false,
      "salvaged": false,
      "last_source_timestamp": 1727998410.10,
      "last_event_at": 1727998410.12
    }
  }
}
```

### 4.3 Atomic Dispatch Protocol with Client-Side Serialization

1. **Process Entry**: Capture `PROCESS_ARRIVAL_TIME_NS = time.time_ns()`. If `is_disabled()` is true, exit 0 immediately.

2. **Step A: Critical Section under File Lock**
   - Acquire non-blocking file lock (`fcntl.LOCK_EX | fcntl.LOCK_NB`) with dynamic deadline `time.monotonic() + min(0.2, max(0.02, time_remaining() - 0.3))`.
     - *Contention rule*: If lock acquisition times out (>200ms or remaining budget), write event envelope (`event_name`, `data`, `context`, `arrival_ns`, `enqueued_ns = time.time_ns()`) to `$STATE_DIR/spool/<enqueued_ns:020d>_<pid>_<monotonic_ns>.json` via atomic rename from `.tmp`. Note that `generation` is omitted from contention envelopes because generation state cannot be reliably read under lock contention. Spool directory is capped at 100 envelopes (protecting close events; only oldest non-close status envelopes are unlinked). Spawn detached background helper (`--reconcile-background`) and exit cleanly.
   - Re-check `is_disabled()` under lock; if true, release lock and exit 0.
   - **Chronological Spool Replay with Poison-Pill Quarantine**:
     - Check `$STATE_DIR/spool/`. Sort files lexicographically by 20-digit zero-padded timestamp.
     - Drain up to 16 pending envelopes per pass in strict FIFO order:
       - On JSON decode or schema validation error: immediately quarantine corrupt envelope to `$STATE_DIR/spool/bad/<filename>` (capped at 20 files) so FIFO is never blocked.
       - If event is a Close event (`pane.closed`, `tab.closed`, `workspace.closed`, or agent exit to `Ended`):
         - Check admission ordering: Apply only if `envelope.get("arrival_ns", envelope.get("enqueued_ns", 0)) >= session.get("admitted_at_ns", 0)` (and if `generation` is present, `envelope.get("generation") == session.get("generation")`). Discard close events predating the current session admission.
         - Record tombstone dict: `data["tombstones"][canonical_pane] = {"closed_at_ns": envelope.get("arrival_ns", envelope.get("enqueued_ns")), "closed_source_ts": max(float(envelope.get("event_data", {}).get("timestamp") or 0.0), session.get("last_source_timestamp", 0.0), float(envelope.get("arrival_ns", envelope.get("enqueued_ns", 0))) / 1e9), "last_source_timestamp": envelope.get("event_data", {}).get("timestamp", 0.0)}`.
         - Stage matching sessions to `desired_state = "Ended"`, increment `seq`.
       - If event is a Status event:
         - Compare source timestamp if present against `last_source_timestamp`. If absent, compare `envelope.get("arrival_ns", envelope.get("enqueued_ns", 0))` against `session.get("last_event_ns", 0)`. If older, drop envelope as superseded.
       - Unlink each spool file ONLY after its mutation is saved into `active-sessions.json`.
   - **Live Event Evaluation**:
     - Validate `canonical_pane_id`: Reject and log if not matching `^[a-zA-Z0-9_:-]{1,48}$`.
     - Check qualification; discard unassisted shell panes.
     - Condition all `context.*` lookups on `is_focused = bool(focused_id and focused_id in (canonical_pane, raw_pane))`.
     - Check tombstone table: if `tomb = data.get("tombstones", {}).get(canonical_pane)`:
       - Extract `tombstone_ns = tomb.get("closed_at_ns") if isinstance(tomb, dict) else int(tomb)`, `t_closed_src = tomb.get("closed_source_ts", 0.0) if isinstance(tomb, dict) else 0.0`, and `last_src_ts = tomb.get("last_source_timestamp", 0.0) if isinstance(tomb, dict) else 0.0`.
       - If `arr_ns <= tombstone_ns` OR (`event.data.timestamp` is present and `<= last_src_ts`): drop event as stale/late arrival (log debug).
       - Else: if `arr_ns - tombstone_ns < 60_000_000_000`:
         - Require ALL positive working admission signals:
           1. `agent_status == "working"` (idle, waiting, blocked, or done states cannot resurrect a recently closed pane).
           2. Source timestamp evaluation:
              - If `src_ts` is not None: `float(src_ts) > t_closed_src and float(src_ts) > last_src_ts`.
              - If `src_ts` is missing / None: `arr_ns > tombstone_ns` with positive working admission.
           3. `event.data.agent` is non-empty.
           4. `is_herdr_alive()` is true.
         - If any condition fails, drop event and preserve tombstone.
         - If all conditions pass: `data.get("tombstones", {}).pop(canonical_pane, None)`.
     - Check agent_exits table: if `canonical_pane in data.get("agent_exits", {})`:
       - Let `exit_info = data["agent_exits"][canonical_pane]`.
       - If `arr_ns <= exit_info.get("exit_at_ns", 0)` OR (`src_ts is not None and src_ts <= exit_info.get("exit_source_ts", 0.0)`): drop event as stale arrival predating agent exit.
       - Else: if `event.data.agent_status == "working"` and `event.data.agent`: `data["agent_exits"].pop(canonical_pane, None)`.
     - If `event.data.timestamp` is present and `< session.get("last_source_timestamp", 0) - 0.1s`: drop event (stale source).
     - **Strict 256 Active Session Capacity Check**:
       - If canonical pane is not yet in `sessions` and `len(sessions) >= 256`:
         - Prune `Ended` sessions and `salvaged` records from `sessions`.
         - If `len(sessions) >= 256` after pruning (all 256 are active live sessions), log error and reject admission of session 257 without evicting active sessions.
     - **Marker Touching Strictly Deferred**: Pane markers in `$STATE_DIR/panes/<hex>` are NOT touched during Step A admission; marker creation and mtime refresh are strictly deferred to Step C upon confirmed HTTP 200 delivery to Bartender. This prevents false health signals if Bartender is unreachable.
     - Update session record:
       - `session["seq"] = session.get("seq", 0) + 1`
       - `session["desired_state"] = mapped_state`
       - `session["desired_payload"] = payload` (includes `"seq": session["seq"]`)
       - `session["last_event_ns"] = arr_ns`
       - `session["last_arrival_ns"] = arr_ns`
       - `session["last_event_at"] = time.time()`
       - `session["delivery_status"] = "in_flight"`
       - `session["delivery_error"] = null`
       - `session["delivery_attempts"] = 0`
       - If new admission or transitioning from `Ended`:
         - Compute `curr_gen = max(data.get("next_generation", 1), data.get("pane_generations", {}).get(canonical_pane, 0), session.get("generation", 0)) + 1`
         - `data["next_generation"] = curr_gen`
         - `data.setdefault("pane_generations", {})[canonical_pane] = curr_gen`
         - `session["generation"] = curr_gen`
         - `session["admitted_at_ns"] = arr_ns`
       - If agent exit to `Ended` (`close_kind == "agent_exit"`):
         - Persist origin on session: `session["exit_at_ns"] = arr_ns`, `session["exit_source_ts"] = float(event.data.get("timestamp") or 0.0)`.
         - Record in agent_exits: `data.setdefault("agent_exits", {})[canonical_pane] = {"exit_at_ns": arr_ns, "exit_source_ts": session["exit_source_ts"]}`.
       - If container close to `Ended` (`close_kind == "container"`):
         - Persist origin on session: `session["closed_at_ns"] = arr_ns`, `session["closed_source_ts"] = max(float(event.data.get("timestamp") or 0.0), session.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9)`.
       - If `event.data.timestamp`: `session["last_source_timestamp"] = event.data.timestamp`
   - **Canonical Lease Takeover Evaluation**:
     - Let `now = time.time()`, `lease_deadline = session.get("lease_deadline", 0)`, `sending_pid = session.get("sending_pid")`.
     - If `sending_pid is not None and sending_pid != os.getpid()`:
       - If `is_pid_alive(sending_pid)`:
         - If `now < lease_deadline + 0.5s`:
           - Prior sender is actively in-flight or in socket drain grace window (<0.5s).
           - Touch `$STATE_DIR/reconciler.pending`. Ensure reconciler is running (`ensure_reconciler_running()`). Save cache atomically, release file lock, and exit cleanly.
         - Else (`now >= lease_deadline + 0.5s`):
           - Prior sender exceeded deadline + grace; hung process takeover. Proceed to claim lease.
       - Else (`not is_pid_alive(sending_pid)`):
         - Prior sender crashed. Proceed to claim lease.
   - **Claim Sending Lease**:
     - Generate unique lease token with process start time: `my_token = f"{os.getpid()}:{PROCESS_START_TIME}:{now}:{session_id}"`.
     - Snapshot `my_resync_gen = session.get("resync_generation", 0)`.
     - Set `session["lease_token"] = my_token`, `session["sending_pid"] = os.getpid()`, `session["lease_deadline"] = now + 1.5`.
     - Save cache atomically and release file lock.

3. **Step B: Network I/O outside Global Lock (Universal Sender Protocol)**
   - Synchronous Step B execution is strictly capped at at most 1 session and at most 2 HTTP POST attempts (1 primary POST + at most 1 minimal retry if rejected) per process. Any additional iterations or pending sessions are deferred to the detached background reconciler.
   - Loop while `session.get("delivered_seq", 0) < session.get("seq", 0)` AND `time_remaining() > 0.3s` (capped at at most 1 iteration in synchronous handler):
     - Snapshot transmission parameters under lease verification:
       - Verify lease ownership: check `session.get("lease_token") == my_token`. If superseded or expired, abort transmission immediately without sending HTTP traffic.
       - `transmitting_payload = session["desired_payload"]`
       - `transmitting_state = session["desired_state"]`
       - `transmitting_seq = session["seq"]`
     - Execute HTTP `POST /event` to `http://127.0.0.1:${NOTCHBAR_AGENTS_PORT:-7823}/event` with socket timeout bounded to `min(0.2, max(0.05, time_remaining() - 0.3))` (<200ms per POST).
     - If `transmitting_state == "Ended"` and request fails or is rejected: automatically retry once with minimal payload `{"state": "Ended", "agent": transmitting_payload.get("agent", "Herdr"), "session_id": session_id}` (only if `time_remaining() > 0.3s`, socket timeout bounded to `min(0.2, max(0.05, time_remaining() - 0.3))`).

4. **Step C: Drain, Reconcile & Evict under File Lock (CPU-Bounded <10ms)**
   - Re-acquire file lock with dynamic deadline `time.monotonic() + min(0.2, max(0.02, time_remaining() - 0.3))`.
     - *Lock Failure Handling*: If lock acquisition times out in Step C, write a delivery result envelope to a dedicated results directory: `$STATE_DIR/results/<timestamp_ns>_<pid>_<seq>.json` containing `version: 1`, `session_id`, `transmitting_seq`, `transmitting_state`, `status` (`success`|`non_retryable`|`retryable`), `error`, `timestamp_ns`, and `pid`. This isolated directory completely separates delivery confirmations from event spooling, preventing collisions with event schemas, quarantines (`spool/bad`), or pruning under the 100-event cap. The background reconciler drains `$STATE_DIR/results/` on every sweep before event spool replay, applying confirmations or staging compensations under lock with staleness checks (`transmitting_seq >= session.get('delivered_seq')`). Launch detached helper (`--reconcile-background`) and exit.
   - Re-check `is_disabled()` under lock; if true, release lock and exit 0.
   - Stage external operations outside lock: initialize `vendor_to_clean = None`, `orphans_to_export = []`, `orphans_to_remove = []`, `compensating_ended = []`.
   - **Persisted Side Effects Guarantee**: Any owed compensating sends or vendor cleanups are recorded with target generation and `admitted_at_ns` in `data["pending_compensations"]` and `data["pending_vendor_cleanups"]` under lock before lock release. After successful dispatch outside lock, the process re-acquires the lock to clear the entry. If the process is terminated by SIGALRM or exit, the background reconciler reads and drains these persisted side effects with identical under-lock re-verification (aborting if a live session was re-admitted) and post-send re-sync, guaranteeing zero lost side effects without phantom session resurrection.
   - Verify lease ownership:
     - Check `session = data.get("sessions", {}).get(session_id)`.
     - If `session is None`:
       - Session was ended and evicted by a concurrent cascade or close handler.
       - **Stale Send Compensation**: If `transmitting_state != "Ended"`, stage compensating `Ended` post outside lock: `compensating_ended.append((session_id, transmitting_payload.get("agent", "Herdr")))` to dismiss the resurrected phantom session. Exit drain loop.
     - If `transmitting_state != "Ended"` and `canonical_pane in data.get("tombstones", {})`:
       - Pane was closed concurrently while non-Ended HTTP delivery was in flight.
       - **Stale Send Compensation with Under-Lock Re-Verification**:
         - Re-verify under lock whether a live session exists: check `active_s = data.get("sessions", {}).get(session_id)`. If `active_s and active_s.get("desired_state") != "Ended"`, abort compensation.
         - Otherwise, stage compensating `Ended` post outside lock: `compensating_ended.append((session_id, transmitting_payload.get("agent", "Herdr")))`. Skip state advancement or marker refresh. Exit drain loop.
     - **Close Sender Confirmation**: If `transmitting_state == "Ended"`, the sender is delivering and confirming closure; it proceeds directly to the Success branch below even if `canonical_pane in tombstones`, advancing `delivered_seq`, evicting the session, deleting the pane marker, and staging vendor cleanup.
     - If `session.get("lease_token") != my_token`:
       - Lease token was superseded by another sender.
       - Bump resync generation: `session["resync_generation"] = session.get("resync_generation", 0) + 1`.
       - Force re-sync to guarantee no dropped updates: `session["delivered_seq"] = 0`, `session["delivery_status"] = "in_flight"`.
       - Touch `$STATE_DIR/reconciler.pending` and call `ensure_reconciler_running()`.
       - Save cache atomically and exit drain loop.
   - Apply response according to Section 3.2 Unified Matrix:
     - On Success (200 with `ok:true`):
       - Check resync generation supersession: if `session.get("resync_generation", 0) > my_resync_gen`:
         - Log debug: Lease resync generation superseded during transmission, forcing re-sync.
         - Set `session["delivered_seq"] = 0`, `session["delivery_status"] = "in_flight"`.
         - Touch `$STATE_DIR/reconciler.pending` and call `ensure_reconciler_running()`.
       - Else:
         - If `transmitting_seq >= session.get("delivered_seq", 0)`:
           - Set `session["delivered_state"] = transmitting_state`.
           - Set `session["delivered_seq"] = transmitting_seq`.
           - Set `session["delivery_status"] = "delivered"`.
           - Set `session["delivery_attempts"] = 0`.
           - Set `data["consecutive_failures"] = 0`.
           - Clear `session["delivery_error"] = null`.
           - Touch/update pane marker mtime: `$STATE_DIR/panes/<hex>`.
           - Clear `$STATE_DIR/DELIVERY_DOWN` and `$STATE_DIR/panes/<hex>.failed`.
           - Stage vendor dismissal: `vendor_to_clean = canonical_pane` (network I/O deferred to post-lock).
           - Re-arm any `"retryable_exhausted"` sessions in cache to `"in_flight"`.
         - **Active Supersession Eviction Check**:
           - If `transmitting_state == "Ended"`:
             - If `session.get("seq") == transmitting_seq`:
               - Evict session from cache (pane closed and confirmed).
               - Remove hex pane marker (`remove_pane_marker(canonical_pane)` unlinks marker and failed flag; does NOT perform network I/O).
               - Stage orphan removal outside lock: `orphans_to_remove.append(session_id)`.
               - Record tombstone dict ONLY if container close (`session.get("close_kind") == "container"`; read persisted Step A origins directly from `session`, never from transient event data): `if session.get("close_kind") == "container": data.setdefault("tombstones", {})[canonical_pane] = {"closed_at_ns": session.get("closed_at_ns") or arr_ns, "closed_source_ts": session.get("closed_source_ts", 0.0), "last_source_timestamp": session.get("last_source_timestamp", 0.0)}`.
               - Record in `agent_exits` table if agent exit (`session.get("close_kind") == "agent_exit"`): `data.setdefault("agent_exits", {})[canonical_pane] = {"exit_at_ns": session.get("exit_at_ns") or arr_ns, "exit_source_ts": session.get("exit_source_ts", 0.0)}`.
             - Otherwise:
               - A new turn arrived (`session.get("seq") > transmitting_seq`)! **Do not evict.** The loop continues and transmits the latest `desired_state`.
     - On Non-Retryable Error (200/ok:false, 3xx, 4xx):
       - Set `session["delivery_status"] = "non_retryable_failed"`.
       - Set `session["rejected_seq"] = transmitting_seq`.
       - Set `session["delivery_error"] = error_str`.
       - Touch `$STATE_DIR/panes/<hex>.failed` so vendor hooks fall through.
       - If `transmitting_state == "Ended"`:
         - **Zero-Data-Loss Rule**: Do NOT evict! Bartender returns 200 for absent sessions; rejection indicates payload error.
         - Mark `session["orphaned_ended"] = true`.
         - Retain session in cache and stage orphan export outside lock: `orphans_to_export.append((session_id, dict(session)))`.
     - On Retryable Failure (5xx, Network Error):
       - Increment `session["delivery_attempts"] += 1`.
       - Set `session["delivery_error"] = error_str`.
       - Touch `$STATE_DIR/panes/<hex>.failed` immediately on first failure.
       - Increment `data["consecutive_failures"] += 1`. If `>= 3`, touch `$STATE_DIR/DELIVERY_DOWN`.
       - If `session["delivery_attempts"] >= 5`:
         - Set `session["delivery_status"] = "retryable_exhausted"`.
         - Touch `$STATE_DIR/panes/<hex>.failed`.
         - If `session.get("desired_state") == "Ended"`:
           - Mark `session["orphaned_ended"] = true`.
           - Retain in cache and stage orphan export outside lock: `orphans_to_export.append((session_id, dict(session)))`.
       - Break in-process loop; schedule background helper (`ensure_reconciler_running()`). Helper releases lease before sleeping and re-claims under lock upon waking.
   - Check if newer state arrived:
     - If `session.get("delivered_seq", 0) < session.get("seq", 0)` AND `time_remaining() > 0.3s` AND `session.get("delivery_status") not in ("non_retryable_failed", "retryable_exhausted")`:
       - Refresh `session["lease_deadline"] = time.time() + 1.5`.
       - Save cache atomically, release lock, execute staged orphan I/O and `cleanup_vendor_active`, and continue loop to Step B.
     - Otherwise:
       - Clear lease: `session["lease_token"] = null`, `session["lease_deadline"] = null`, `session["sending_pid"] = null`.
       - If `session.get("delivered_seq", 0) < session.get("seq", 0)` and not non_retryable: spawn background reconciler (`ensure_reconciler_running()`) before exit.
       - Save cache atomically, release lock.
       - **Post-Lock External Dispatch (<10ms CPU lock guarantee)**:
         - Execution budget gate: All external network dispatches require `time_remaining() > 0.3s`. If remaining execution budget <= 0.3s, external dispatches (compensations, vendor cleanups) are deferred to the background reconciler via `touch_reconciler_pending()` and `ensure_reconciler_running()`.
         - Execute staged compensating `Ended` sends outside lock via `_raw_post_event` (if `time_remaining() > 0.3s`).
         - **Post-Compensation Re-Sync Detection**: If compensating `Ended` was sent, re-acquire file lock; if an active session was admitted while compensation was in flight (`active_s and active_s.get("desired_state") != "Ended"`), force `active_s["delivered_seq"] = 0`, `active_s["delivery_status"] = "in_flight"`, touch `$STATE_DIR/reconciler.pending`, and ensure background reconciler is running. Clear pending compensation from `pending_compensations` under lock.
         - Execute staged orphan file removals (`remove_orphan_record`) and exports (`export_orphan_record`) under dedicated `.herdr-bartender-orphans.json.lock`.
         - Dispatch staged `cleanup_vendor_active(vendor_to_clean)` outside lock (if `time_remaining() > 0.3s`), and re-acquire lock to clear the entry from `pending_vendor_cleanups`. If budget <= 0.3s, defer to background reconciler.
         - Exit drain loop.
   - **Unconditional Watchdog Check**: If any non-Ended session remains in cache, ensure background watchdog is running (`ensure_reconciler_running()`).

5. **Multi-Session Cascade Protocol (`tab.closed`, `workspace.closed`)**:
   - Step A acquires lock once:
     - Validates container ID against `^[a-zA-Z0-9_:-]{1,48}$`.
     - Identifies matching sessions in `active-sessions.json`.
     - For each matching session:
       - Checks spool generation and admission timestamp: ignores close events older than current generation or predating admission.
       - Increments `seq`, sets `desired_state = "Ended"`, `closed_at_ns = arr_ns`, `close_kind = "container"`.
       - Records tombstone dict: `data.setdefault("tombstones", {})[canonical_pane] = {"closed_at_ns": arr_ns, "closed_source_ts": max(float(event_data.get("timestamp") or 0.0), session.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9), "last_source_timestamp": session.get("last_source_timestamp", 0.0)}`.
       - Evaluates lease:
         - If active lease held by another alive PID (< deadline + 0.5s): marks pending (`touch_reconciler_pending()`, `ensure_reconciler_running()`).
         - Otherwise: claims lease (`lease_token = f"{pid}:{PROCESS_START_TIME}:{now}:{sid}"`, `lease_deadline = now + 1.5`, `sending_pid = pid`) and queues session for transmission.
     - Clamps synchronous deliveries to **at most 1 session inline (<200ms)**:
       - `sync_sessions = target_sessions[:1]`.
       - If matching sessions exceed 1 (`overflow_sessions = target_sessions[1:]`):
         - Clears claimed leases on overflow sessions (`sending_pid = null, lease_token = null, lease_deadline = null`).
         - Calls `ensure_reconciler_running()` to delegate overflow sessions immediately to the detached background reconciler (`--reconcile-background`), protecting the 1.5s process watchdog.
     - Saves cache atomically and releases lock.
   - Step B iterates through `sync_sessions` outside lock, executing HTTP `POST /event` (`{"state": "Ended", ...}`) with minimal payload fallback on rejection (`assert not IN_CRITICAL_SECTION`).
   - Step C re-acquires lock once:
     - Stage external operations: `orphans_to_export = []`, `orphans_to_remove = []`, `vendors_to_clean = []`, `compensating_ended = []`.
     - For each delivered session:
       - If `s.get("lease_token") != my_token`: forces re-sync (`delivered_seq = 0, delivery_status = "in_flight"`), touches pending, calls `ensure_reconciler_running()`, and skips eviction.
       - Clears lease fields (`sending_pid = null, lease_token = null, lease_deadline = null`).
       - On success:
         - If `s.get("desired_state") == "Ended" and s.get("seq") == target_seq`:
           - Evicts session from cache.
           - Removes pane marker (`remove_pane_marker(canonical_pane)` only unlinks markers/failed flags; no network I/O).
           - Stages orphan removal: `orphans_to_remove.append(sid)`.
           - Re-affirms tombstone dict ONLY if container close (`s.get("close_kind") == "container"`): `if s.get("close_kind") == "container": data.setdefault("tombstones", {})[canonical_pane] = {"closed_at_ns": s.get("closed_at_ns") or arr_ns, "closed_source_ts": max(float(event_data.get("timestamp") or 0.0), s.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9), "last_source_timestamp": s.get("last_source_timestamp", 0.0)}`.
           - Stages `vendors_to_clean.append((canonical_pane, True))`.
       - On non-retryable rejection (or retryable error after 5 attempts):
         - Marks `s["orphaned_ended"] = true`.
         - Stages orphan export: `orphans_to_export.append((sid, dict(s)))`.
         - Retains session in cache (Zero-Data-Loss).
     - Saves cache atomically and releases lock.
     - **Post-Lock External Dispatch**:
       - Dispatches compensating `Ended` requests outside lock if any session was tombstoned during delivery.
       - Executes staged orphan exports and removals under orphan file lock.
       - Dispatches `cleanup_vendor_active(pane, is_pane_closed=True)` outside lock.

---

## 5. Reconciliation Driver, Budgets & Expiry

### 5.1 Reconciliation Driver & Helper Singleton
1. **Universal Sender Protocol Enforcement**:
   - The background reconciler (`--reconcile-background`), opportunistic in-process sweep, and `--cleanup` MUST execute the identical Step A / Step B / Step C lease claim, transmitting snapshot, and token verification protocol as the primary event path.
2. **Periodic Watchdog Daemon (`--reconcile-background`)**:
   - Spawns unconditionally whenever an event leaves one or more active non-Ended sessions (`Working`, `Waiting`, `Idle`, `Done`), or on lock contention spooling.
   - Exempt from short 1.5s process watchdog.
   - **Singleton Guard with 20s Watchdog Cadence**:
     - Acquires non-blocking file lock on `$STATE_DIR/reconciler.lock`. If locked, spawners touch `$STATE_DIR/reconciler.pending` and exit.
     - While holding lock, loops while `not is_disabled()`:
       1. Drains results envelopes and event spools:
          - 1a. Drains `$STATE_DIR/results/` (`<timestamp_ns>_<pid>_<seq>.json`) in strict chronological order: applies delivery confirmations (`seq >= delivered_seq`) or stages compensations under lock, removing each file only after its mutation is saved into cache. Completely isolated from event spooling to eliminate schema quarantine (`spool/bad`) or 100-cap pruning collisions.
          - 1b. Replays and unlinks files in `$STATE_DIR/spool/` in strict FIFO order (up to 16 envelopes per pass, quarantining corrupt envelopes to `$STATE_DIR/spool/bad/<filename>`).
       2. Sweeps undelivered sessions in `active-sessions.json` using the Universal Sender Protocol:
          - On Step C lease mismatch (`lease_token != my_token`), forces re-sync (`delivered_seq = 0, delivery_status = "in_flight"`).
          - On confirmed HTTP 200 delivery for `Ended`, removes session from cache, marker, and orphan file (`remove_orphan_record(sid)`), and dispatches staged `cleanup_vendor_active` outside the lock.
          - On non-retryable rejection or 5th retryable failure for `Ended`, marks `orphaned_ended = true`, mirrors to canonical orphan file (`export_orphan_record(sid, s)`), and retains in cache during active retry horizons.
       3. **Pending Trigger Lifecycle & Responsive 0.5s Sleep Ticks**:
          - `$STATE_DIR/reconciler.pending` is unlinked immediately before the sweep pass.
          - If new signals or contention envelopes arrive during the sweep, `reconciler.pending` is re-created, resetting `bartender_absent_since = None` and triggering an immediate subsequent pass.
          - Between sweep cycles, the reconciler sleeps in **0.5-second ticks**:
            `while time.time() < sleep_deadline and not is_disabled(): if pending_file.exists() or (spool_dir.exists() and any(spool_dir.glob("*.json"))): bartender_absent_since = None; break; time.sleep(0.5)`.
            This guarantees sub-0.5s wakeup upon arrival of deferred deliveries, satisfying the <=1.0s latency criterion under all operating conditions.
       4. **Quiet Outage Health Recovery & Hardened Orphan Replay**:
          - If any session is `retryable_exhausted` or `DELIVERY_DOWN` exists, probes `GET /health`. On success, clears `DELIVERY_DOWN`, resets `delivery_attempts = 0`, sets `delivery_status = "in_flight"`, and triggers immediate delivery.
          - When `/health` succeeds and `$HOME/.herdr-bartender-orphans.json` exists, automatically invokes orphan replay (`run_replay_orphans()`) under `.herdr-bartender-orphans.json.lock`.
          - **Single Normative Orphan Replay Guard**: An orphan record `s` for `session_id` is skipped iff: `active_s = cache.sessions.get(session_id)` (or matching `pane_id`) satisfies `active_s is not None and not active_s.get('salvaged', False) and active_s.get('desired_state') != 'Ended'`. Otherwise, orphan replay transmits `Ended` to Bartender and removes the orphan record upon confirmed delivery. Salvaged sessions (`salvaged == True`) and epoch-dominating salvage generations (`>= 1_700_000_000`) NEVER satisfy this predicate and strictly never suppress orphan replays.
       5. **Marker Heartbeat for All Active States (including Idle and Done)**: Sweeps all active non-Ended sessions (`desired_state != "Ended"` and `delivery_status == "delivered"`, including `Idle`, `Done`, `Working`, and `Waiting`), strictly excluding salvaged records: touches `$STATE_DIR/panes/<hex>` every 20 seconds while Herdr is alive (`is_herdr_alive()`), ensuring 60s freshness is strictly maintained across arbitrarily long turns or pauses between turns (>60s). If Herdr process exits (dead), marker refresh stops immediately.
       5a. **Drain Persisted Compensations & Vendor Cleanups**: Reads and drains `pending_compensations` and `pending_vendor_cleanups` under lock with under-lock re-verification (aborting if a live session was re-admitted) and post-send re-sync, guaranteeing zero lost side effects.
       5b. **Vendor Dismissal Queue (`dismissed_vendor_uuids`) Lifecycle**:
           - Reconciler sweeps `dismissed_vendor_uuids` in cache (capped at 64 entries, pruning oldest).
           - For each UUID: if integration is disabled (`$STATE_DIR/DISABLED` or `(state_dir / "NO_HOOKS").exists()`), or vendor fallback is active (`$STATE_DIR/panes/<hex>.vendor_active` exists, or `.failed` exists, or `not is_herdr_alive()`): the queued dismissal is immediately CANCELLED and purged without sending `Ended`, protecting active vendor fallback representations.
           - Otherwise, sends `Ended` to Bartender (timeout <=0.2s) across a 10-second settling window, retrying every 2s until >=10s elapsed or 3 attempts; on confirmed HTTP 200, or after 3 failed attempts, or once >=10s has elapsed, purges the UUID from `dismissed_vendor_uuids`. If Herdr delivers again subsequently, `cleanup_vendor_active` re-queues the dismissal.
           - Otherwise, sends `Ended` to Bartender (timeout <=0.2s) across a 10-second settling window, retrying every 2.0s up to 5 attempts across the 10s settling window; on confirmed HTTP 200, or after 5 failed attempts, or once >=10s has elapsed, purges the UUID from `dismissed_vendor_uuids`.
          - Sweeps temporary stdin capture files (`.guard_stdin.*`) in `$STATE_DIR` older than 60s and unlinks them.
          - Does NOT blind-sweep `.vendor_active` while Herdr is dead (preserving active vendor fallback). Bare `.vendor_active` markers are unlinked strictly on confirmed Herdr delivery or pane close, and UUID-bearing `.vendor_active` files are cleaned up strictly via `cleanup_vendor_active` or when exceeding 12 hours.
       7. Checks TTL expirations:
          - `Salvaged` sessions: Expire after a 5-minute (300s) quiescent horizon (or immediately if Herdr is dead, `now_wall - last_ts > 300 or not is_herdr_alive()`).
          - `Working`: Expire after 12 hours.
          - `Waiting`: Expire after 48 hours.
          - `Idle` and `Done`: Expire after 24 hours.
          - Herdr dead >5 minutes: Expire all sessions immediately to `Ended`.
       8. Checks Bartender restart via `get_bartender_pid()` searching `["Bartender 6", "Bartender"]` selecting process with earliest start time. Tracks `last_bartender_pid` and `last_bartender_start_time` in cache. A restart triggers Full Top Shelf Re-Sync if the PID changes or if start time changes on the same PID (PID reuse defense).
       9. Checks Herdr instance restart via `get_herdr_pid()` prioritizing GUI `.app` bundles, selecting earliest start time. Tracks `last_herdr_pid` and `last_herdr_start_time` in cache. A restart triggers mass session expiry if the PID changes or if start time changes on the same PID (PID reuse defense).
       10. **Automatic Guard Integrity Gated on SHA Allowlist**: Gated on `if not is_disabled() and not (state_dir / "NO_HOOKS").exists():`, verifies vendor hooks still have the dedup guard block. Before re-patching, verifies the clean SHA-256 hash of each unpatched vendor hook script against `vendor-hook-sha.json` (SHA allowlist). If the clean hash matches a known vendor release, re-patches the hook cleanly. If any vendor hook's clean hash does not match (indicating an unverified upstream update or manual modification), automatic re-patching is strictly skipped, a notification flag `$STATE_DIR/HOOK_NEEDS_REVIEW` is touched, `.hook_review_alerted` is touched, an active macOS notification alert is dispatched via `osascript`, and a warning is logged, preventing unsafe blind re-patching. While unpatched, the integration operates in degraded fail-open mode, permitting native vendor hooks to handle notifications directly. If `$STATE_DIR/NO_HOOKS` exists (sticky uninstall), automatic re-patching is strictly skipped.
       11. **Bartender Presence Tracking, Wall-Clock Absence Horizon & Dynamic Backoff**:
           - Tracks `bartender_absent_seconds` using wall-clock elapsed time when `get_bartender_pid()` returns `None`.
           - If Bartender is running or `reconciler.pending` is touched, absence counter resets to 0.
           - If absent for >1000s (~16 minutes), sleep interval backs off from 20s to **300s** (still waking in 0.5s ticks if pending signals arrive).
           - **Terminal Horizon**: If Bartender has been absent for **>12 hours (43200s)** and Herdr is dead (`not is_herdr_alive()`), the reconciler exports all undelivered sessions to `$HOME/.herdr-bartender-orphans.json` (mode `0600`), logs clean termination, and exits cleanly.
           - Otherwise, if active, exhausted, or undelivered sessions remain, sleeps for the calculated interval. If idle for 60 consecutive seconds with 0 sessions and healthy bridge, exits cleanly.
       12. **Cache Capacity & Log Hygiene (Strict Active Session Protection)**:
           - Active sessions cache is capped at 256 sessions: capacity pruning targets **only** `Ended` or `salvaged` records. Active live sessions (`Working`, `Waiting`, `Idle`, `Done` with `not salvaged`) are **never** evicted. If 256 live active sessions exist, admission of session 257 is rejected with an error log.
           - `pane_generations` is capped at 512 entries, pruning inactive pane keys by lowest generation while root `next_generation` monotonically advances.
           - Safe lock discipline: orphan file I/O and process spawns are staged under lock and dispatched outside lock (<10ms CPU lock guarantee).
           - Sessions undelivered for >12 hours past TTL or unconfirmed Ended sessions reaching the terminal absence horizon are exported to canonical orphans and evicted from `active-sessions.json`.
           - `log_debug` automatically rotates `$STATE_DIR/plugin.log` to `plugin.log.1` when exceeding 1MB (maximum 2MB total disk footprint).
3. **Retry Schedule & Long-Tail Recovery**:
   - Fast retry schedule: 5 attempts with fixed exponential backoff (0s, 1s, 2s, 4s, 8s, total ~15s).
   - Helper does NOT hold lease across retry backoff sleeps: leases are released before sleeping and re-claimed under lock with lease token verification upon waking.
   - If 5 attempts fail: session marked `delivery_status = "retryable_exhausted"`, marker removed, `<hex>.failed` touched. If `desired_state == "Ended"`, marks `orphaned_ended = true`, retains in cache, and exports to canonical orphan file.
   - When `/health` succeeds or any subsequent event POST succeeds, all `retryable_exhausted` sessions are automatically reset to `delivery_attempts = 0` and `delivery_status = "in_flight"` for immediate delivery.
4. **Bridge Restart Detection & Full Top Shelf Re-Sync**:
   - Detected client-side when Bartender's PID changes or after bridge reconnection following `DELIVERY_DOWN`.
   - Triggers a **Full Top Shelf Re-Sync**: sets `delivered_seq = 0` for all active non-Ended sessions in cache, re-asserting all states on Top Shelf.
   - **Salvaged sessions are strictly excluded from re-sync**: They are never transmitted as `Idle` (prevents overwriting real states). They remain quiescent until a live Herdr event supplies real state.

### 5.2 Cascade & Cleanup Budgets
- External Herdr plugin execution limit: `2.0s`.
- Process hard watchdog (SIGALRM): `1.5s`.
- Default per-request socket timeout: `min(0.2, max(0.05, time_remaining() - 0.3))` (<200ms).
- `pane.closed`: 1 session, socket timeout `<0.2s` (`min(0.2, max(0.05, time_remaining() - 0.3))`).
- Container cascades (`tab.closed` & `workspace.closed`): Synchronous deliveries are clamped to **at most 1 session inline (<200ms)** (`target_sessions[:1]`). Any additional matching sessions (`target_sessions[1:]`) are immediately delegated to the detached background reconciler (`--reconcile-background`), guaranteeing the 1.5s watchdog is never breached.
- `--cleanup`: Sweeps all sessions with 0.15s per-session timeout (budget scales to `max(10.0, len(sessions) * 0.15)`). Explicitly exempt from 1.5s watchdog. Explicitly bypasses `$STATE_DIR/DISABLED`.
  - **Exit Code Contract**:
    - Exit `0`: All sessions confirmed Ended via bridge (HTTP 200).
    - Exit `2`: Bridge unreachable; pending sessions exported to canonical `$HOME/.herdr-bartender-orphans.json` (outside state directory, mode `0600`).
    - Exit `1`: Fatal error during cleanup.

---

## 6. Deadlines, Signal Safety & Corrupt Cache Handling

### 6.1 Process Deadlines & Signal Safety
- **Hard Deadline Enforcement**: Enforced strictly via `signal.setitimer(signal.ITIMER_REAL, 1.5)`.
- **Bounded Operations**:
  - Direct connection to literal loopback IP `127.0.0.1` (no blocking DNS resolution).
  - Sockets use explicit dynamic timeouts bounded to `min(0.2, max(0.05, time_remaining() - 0.3))` (<200ms per attempt).
  - Disk operations use local APFS fsync (<5ms).
  - Orphan lock acquisition uses non-blocking try (`fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` with 50ms deadline); if contended, orphan exports remain in cache and are delegated to the background reconciler.
- **Dynamic Shared Budget & Cascade Clamping Guarantee**:
  - All network I/O operations (HTTP POST, retries, vendor cleanups) draw from a shared dynamic budget: `socket_timeout = min(0.2, max(0.05, time_remaining() - 0.3))`. If remaining time drops to <= 0.3s, network transmission is skipped and deferred to the background reconciler.
  - Container cascades (`tab.closed`, `workspace.closed`) synchronously dispatch **at most 1 session inline (<200ms)**; all remaining sessions (>1) are left `in_flight` under cache lock (<10ms) and handed off immediately to the detached background reconciler (`--reconcile-background`), which runs without the 1.5s plugin supervisor deadline.
  - Subprocess liveness lookups (`get_bartender_pid()`, `is_herdr_alive()`) are cached for 0.5s during process execution, avoiding redundant `pgrep` forks.
- **Comprehensive Worst-Case Execution Budget Table (<1.5s Guarantee)**:
  | Operation Phase | Worst-Case Bound | Typical / Normal Bound | Notes |
  | :--- | :--- | :--- | :--- |
  | Process startup & module load | 40ms | < 20ms | Local Python 3 runtime initialization |
  | Process liveness check (`pgrep` + `ps -o lstart`) | 35ms | < 5ms | Cached for 0.5s during process run |
  | Initial file lock acquisition | 150ms | < 5ms | Bounded backoff loop |
  | Results directory drain & spool replay | 100ms | 0ms | Batched single atomic save |
  | Step A critical section (parse + mutate) | 10ms | < 2ms | CPU-bounded JSON dump + atomic replace |
  | Step B HTTP POST (single session, max 1 attempt) | 200ms | 2–5ms | Dynamic socket timeout `min(0.2, max(0.05, time_remaining() - 0.3))` |
  | Step B minimal-payload retry (if rejected) | 200ms | 0ms | Triggered only on rejection of `Ended` if `time_remaining() > 0.3s` |
  | Step C re-acquire lock & atomic swap | 150ms | < 5ms | Bounded backoff loop |
  | Post-lock dispatch (compensating POST / cleanup) | 200ms | 0ms | Dynamic socket timeout `min(0.2, max(0.05, time_remaining() - 0.3))` |
  | Post-compensation lock clearing | 20ms | < 2ms | Re-locks cache briefly to clear pending side effects |
  | Non-blocking orphan lock check | 10ms | < 1ms | `fcntl.flock` with `LOCK_NB` |
  | Background reconciler spawn (`Popen`) | 20ms | < 2ms | `start_new_session=True` Popen fork |
  | **Total Worst-Case Maximum Execution** | **1135ms** | **< 25ms** | **Strictly fits within 1500ms real-time deadline (365ms safety headroom)** |
- **Signal Safety & Non-Critical Exits**:
  - The SIGALRM signal handler sets `PENDING_WATCHDOG_EXIT = True`. Zero file I/O is performed in signal handler context during critical sections.
  - Critical sections are strictly CPU-bounded (<10ms for JSON dump + `os.replace`). If SIGALRM fires during a critical section (`IN_CRITICAL_SECTION == True`), exit is deferred until `__exit__` completes the atomic swap and releases the lock, at which point the process cleanly exits 0.
  - When outside critical sections, SIGALRM triggers non-blocking background reconciler launch if unconfirmed sessions remain and exits 0 cleanly.

### 6.2 Atomic Cache Replacement & Quarantine
Every cache mutation follows atomic replacement semantics:
1. Serialize JSON data to temporary file: `active-sessions.json.tmp.<pid>`.
2. Flush and force to disk: `f.flush(); os.fsync(f.fileno())`.
3. Atomically replace target: `os.replace(tmp_path, cache_file)`.
4. Stale temporary files (`.tmp.*`) older than 60s are swept and unlinked on startup.

### 6.3 Fully Specified Corrupt Cache Quarantine & Salvage
If `active-sessions.json` cannot be decoded:
1. Move corrupt file: `os.replace(cache_file, cache_file.with_name(f"active-sessions.json.corrupt.{int(time.time())}"))`. Prune quarantine files older than 7 days.
2. **Selective Spool Preservation & Quarantine**: All pending `.json` envelopes in `$STATE_DIR/spool/` are inspected. Close envelopes (`pane.closed`, `tab.closed`, `workspace.closed`, or agent exit to `Ended`) are **preserved** in `$STATE_DIR/spool/` to be replayed against the salvaged cache. Only non-close status envelopes are quarantined to `$STATE_DIR/spool/bad/` (capped at 20 files). This prevents orphaned phantom entries from surviving on Top Shelf if a crash occurred during pane closure.
3. **Non-Destructive Salvage**: Raw text scan extracting candidate IDs: `r'herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}'`, strictly validated against anchored regex `^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$`.
4. For each candidate ID:
   - Assigns salvaged sessions an epoch-dominating generation counter: `salvage_epoch_gen = max(int(time.time()), 1_700_000_000)`. Stored in both `pane_generations[canonical_pane]` and `session['generation']`, and updates `next_generation = max(next_generation, salvage_epoch_gen)`. This epoch-dominating integer strictly dominates any counter-based pre-crash generation, guaranteeing that subsequent live agent turns compute `curr_gen >= salvage_epoch_gen + 1`, preventing stale spooled envelopes from older turns from superseding active sessions.
   - All salvaged sessions stage quiescently as `"Idle"` without heuristic regex text search (`desired_state = "Idle"`, `delivered_state = "Idle"`, `seq = 1`, `delivered_seq = 1`, `delivery_status = "salvaged"`, `salvaged = True`).
   - Deletes any existing pane markers (`remove_pane_marker(canonical_pane)`) to ensure unconfirmed salvaged records never emit false health signals to dedup guards.
   ```json
   {
     "pane_id": "<canonical_pane_id>",
     "workspace_id": "<extracted_workspace_id_or_null>",
     "tab_id": null,
     "host": "<sanitized_host>",
     "agent": "Herdr",
     "raw_agent": null,
     "title": "Salvaged Session <pane_id>",
     "cwd": "",
     "desired_state": "Idle",
     "delivered_state": "Idle",
     "seq": 1,
     "delivered_seq": 1,
     "rejected_seq": 0,
     "desired_payload": {
       "state": "Idle",
       "agent": "Herdr",
       "session_id": "<candidate_id>",
       "seq": 1
     },
     "delivery_status": "salvaged",
     "delivery_error": null,
     "delivery_attempts": 0,
     "salvaged": true,
     "admitted_at_ns": 1727998410000000000,
     "last_event_ns": 1727998410000000000,
     "last_arrival_ns": 1727998410000000000,
     "last_event_at": 1727998410.12,
     "last_applied_arrival_time": 1727998410.12
   }
   ```
5. **Safety & Quiescent Model Rules**:
   - Salvaged sessions are staged in a quiescent state (`desired_state = "Idle"`, `delivered_state = "Idle"`, `seq = 1`, `delivered_seq = 1`, `delivery_status = "salvaged"`).
   - **Exclusion from automatic transmission & marker heartbeat**: Salvaged records are NEVER automatically transmitted on bridge re-sync or periodic reconciler sweeps, and are excluded from periodic marker mtime touch, preventing phantom agent states from appearing on Top Shelf or suppressing vendor hooks without live confirmation.
   - **Re-activation on live events**: When a real Herdr event arrives for a salvaged pane, it advances `seq`, populates live agent metadata, clears `salvaged = False`, and transmits the real state normally.
   - **Bounded Quiescent Horizon (5 Minutes)**: If no live events arrive, salvaged sessions expire to `Ended` after a deterministic 5-minute (300s) quiescent horizon (or immediately if Herdr is dead, `now_wall - last_ts > 300 or not is_herdr_alive()`), rather than lingering for a 24h TTL, and are cleanly evicted during `--cleanup`.
6. Write fresh cache atomically under held lock.

---

## 7. Per-Pane Ownership Hook Guard & Installation

### 7.1 Dedup Guard with Handover Tracking
The hook guard decouples dedup suppression from network delivery traffic by checking per-pane tracking markers combined with Herdr process liveness, freshness, and delivery health:
- **Marker Directory**: `$XDG_STATE_HOME/herdr/plugins/herdr-bartender/panes/`.
- **Filename**: `<hex_encoded_pane_id>` computed over the single canonical pane string (`HEX_PANE = canonical_pane_id.encode('utf-8').hex()`).
  - Example: `default:p1` &rarr; `64656661756c743a7031`.
  - Contents: Unix epoch timestamp of last confirmed delivery / refresh.
- **Suppression Invariants & Handover Protocol**:
  - The vendor hook exits 0 (suppressing native vendor notifications for that pane) **if and only if ALL** of the following 7 conditions hold:
    1. Integration is not disabled (`$STATE_HOME/DISABLED` absent).
    2. Bridge delivery is not down (`$STATE_HOME/DELIVERY_DOWN` absent).
    3. `$HERDR_PANE_ID` matches `^[a-zA-Z0-9_:-]{1,48}$`.
    4. Per-pane marker `$STATE_HOME/panes/<hex>` exists.
    5. Per-pane marker is **fresh** (updated within the last 60 seconds).
    6. Per-pane failure flag `$STATE_HOME/panes/<hex>.failed` is absent.
    7. Herdr process is actively running (`pgrep -xi "herdr"`).
  - **Preserved `.vendor_active` Handover Tracking without Blind Expiry**: `.vendor_active` files are NOT blindly expired after 60s. Instead, the guard touches `.vendor_active` on passthrough whenever a non-session-terminal event occurs, maintaining ownership throughout multi-turn sessions. The marker is unlinked ONLY on:
    1. A vendor session-terminal event (`Ended`, `SessionEnd`).
    2. Confirmed delivery via `cleanup_vendor_active` (which unlinks bare touches to restore dedup, and sends `Ended` plus unlinks for valid UUID records).
    3. Non-terminal event under healthy Herdr for a bare touch (`_HB_VA_HAS_UUID -eq 0`), which unlinks `.vendor_active` and exits 0, restoring dedup.
    4. Pane closure (`cleanup_vendor_active(pane, is_pane_closed=True)`).
    5. Reconciler 12-hour horizon sweep (without blind sweeps while Herdr is dead).
  - **Event Pre-Classification & Argv JSON Payload Extraction**: Inspects `$1` first. If `$1` provides terminal state (`Ended`, `SessionEnd`, `Stop`, `Done`, `AgentDone`, `AgentWaiting`), or if `$1` starts with `{` (OpenAI Codex CLI style JSON payload), the guard extracts `session_id` and classifies `hook_event_name`/`event`/`state` directly from `$1`. If `$1` classifies as session-terminal (`Ended`, `SessionEnd`, `session-end`), stdin reading is skipped entirely.
  - **Bounded Byte-Exact Stdin Spooling with Replay-plus-Remainder Splice**: For non-terminal events with piped stdin (`! -t 0`), stdin capture is strictly bounded to <=1.0s (unbounded payload size) using `/usr/bin/perl -e '$SIG{ALRM} = sub { exit 142 }; alarm 1; while (sysread(STDIN, my $b, 65536)) { print $b; } alarm 0; exit 0;'` (with python3 `signal.alarm(1)` fallback). Chunk size of 64KB is the read buffer, not a cap; input of arbitrary size captured within 1.0s is preserved byte-exact without truncation.
    - When capture times out or errors (`_HB_CAPTURE_ERR -ne 0`): if partial input was captured in `_HB_GUARD_TMP`, the guard creates an unlinked temporary FIFO (`mkfifo $_HB_SPLICE_FIFO`), spawns a background feeder subshell `( cat "$_HB_GUARD_TMP"; rm -f "$_HB_GUARD_TMP"; exec cat ) > "$_HB_SPLICE_FIFO" 2>/dev/null &`, redirects `exec < "$_HB_SPLICE_FIFO"`, and unlinks the FIFO path immediately. This feeds the captured prefix followed by the remaining stdin stream without truncating input, while preventing Bash from waiting on process substitution (`<()`) subshells upon script exit. If no bytes were captured, `_HB_GUARD_TMP` is unlinked and cleared, leaving stdin untouched for the vendor script. In both cases, error sets `_HB_CAPTURE_ERR -ne 0`, which blocks suppression (`[ "$_HB_CAPTURE_ERR" -eq 0 ] && [ "$_HB_HERDR_HEALTHY" -eq 1 ]`) and **fails open** immediately to vendor hook.
    - If `mktemp` fails, or if stdin is a TTY or empty pipe and no token or JSON payload is provided in `$1` (`[ -z "${1:-}" ]`), `_HB_CAPTURE_ERR=1` is set immediately to fail open cleanly without hanging or suppressing.
    - Pure bash fallback (when neither perl nor python3 is available): bash cannot safely buffer unbounded multiline stdin with a timeout without line truncation; bash unlinks `_HB_GUARD_TMP`, sets `_HB_GUARD_TMP=""`, sets `_HB_CAPTURE_ERR=1`, and classifies using argv `$1` only, leaving stdin completely untouched for the vendor script.
    - When valid stdin is captured: redirects via `exec < "$_HB_GUARD_TMP"; rm -f "$_HB_GUARD_TMP"`, immediately releasing the file inode while preserving the open file descriptor for the vendor process, avoiding `trap ... EXIT` and avoiding process substitution `<(...)`.
  - **Shell Interpreter Requirements**: Pinned strictly to `/usr/bin/env bash` (standard on macOS) supporting `local` and POSIX utilities.
  - **Turn-Terminal vs Session-Terminal Separation & Bounded Vendor Dismissal**:
      - Inspects argv `${1:-}` and stdin JSON payload (`hook_event_name`, `event`, or `state`):
        - Session-terminal events: `Ended`, `SessionEnd`, `session-end`.
        - Turn-terminal events: `Stop`, `Done`, `AgentDone`, `AgentWaiting`, `agent-turn-complete`.
      - Session-terminal precedence: If `_HB_IS_SESSION_TERMINAL -eq 1` (`Ended`, `SessionEnd`, `session-end`), `.vendor_active` is unlinked immediately and the event passes through to the vendor script to dismiss any vendor Top Shelf entry.
      - If `.vendor_active` exists:
        - If `_VA_HAS_UUID -eq 0` (bare touch):
          - Turn-terminal events (`Stop|Done|AgentDone|AgentWaiting|agent-turn-complete`) pass through to vendor without unlinking `.vendor_active`.
          - Non-terminal events under healthy Herdr: suppress (`exit 0`) and unlink `.vendor_active`, restoring dedup.
          - Non-terminal events under unhealthy Herdr: pass through to vendor without unlinking.
        - If `_VA_HAS_UUID -eq 1` (extracted UUID exists):
          - Turn-terminal (`Stop|Done|AgentDone|AgentWaiting|agent-turn-complete`): passes through to vendor without error, preserving `.vendor_active`.
          - Non-terminal: checks the 7-condition Herdr health predicate; suppresses vendor if Herdr is healthy, otherwise maintains `.vendor_active` with session ID atomically via `.va.tmp`.
      - If `.vendor_active` is absent:
        - Session-terminal events (`Ended`, `SessionEnd`, `session-end`): NEVER suppressed (`: # pass through to vendor script`), safely dismissing any stranded vendor entries without affecting Herdr state.
        - Non-terminal events: if Herdr owns the pane (7-condition predicate holds), suppresses vendor (`exit 0`).
        - If Herdr is unhealthy or does not own the pane: passes through to vendor and records `.vendor_active` (with extracted UUID or bare touch) for non-session-terminal events.
    - **Codex CLI Lifecycle**: OpenAI Codex CLI passes JSON payloads in argv (`$1`) or stdin. Argv JSON payload extraction inspects `$1` for `"session_id"` and `"hook_event_name"`, detecting `session-end` or `agent-turn-complete`. If no explicit session termination event is received, dismissal of native Codex entries is authoritatively handled by Herdr agent exit (`pane.agent_status_changed` with empty agent) or container closure (`pane.closed`), both of which trigger `cleanup_vendor_active`.
    - If the user closes the pane in Herdr, `remove_pane_marker` unlinks marker and failed flag under lock; after lock release, `cleanup_vendor_active(canonical_pane, is_pane_closed=True)` sends `Ended` for any recorded `vendor_session_id` and unlinks `.vendor_active` (including bare touches on pane close).
  - **Reconciler Watchdog Heartbeat**: While any session is active, the background reconciler runs on a **20-second cadence**, updating marker freshness for all live panes with confirmed delivery (including Idle and Done sessions, strictly excluding salvaged sessions) so long-running turns or inter-turn pauses (>60s) never suffer premature fall-through.
  - On the **very first delivery failure** for a pane, Herdr touches `<hex>.failed`, enabling immediate fall-through to vendor hooks without waiting for retry exhaustion.
  - On 3 consecutive global bridge communication failures, `$STATE_DIR/DELIVERY_DOWN` is touched.

### 7.2 Standalone Hook Guard Implementation
Delimited block installed in `claude-event-hook.sh` and `codex-notify-hook.sh` immediately following `set -u` (or shebang):

```bash
# BEGIN HERDR-BARTENDER DEDUP GUARD
if [ -n "${HERDR_PANE_ID:-}" ]; then
  if printf '%s' "$HERDR_PANE_ID" | grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
    _HB_CANONICAL_PANE=""
    if printf '%s' "$HERDR_PANE_ID" | grep -q ':'; then
      _HB_CANONICAL_PANE="$HERDR_PANE_ID"
    elif [ -n "${HERDR_WORKSPACE_ID:-}" ]; then
      _HB_CANONICAL_PANE="${HERDR_WORKSPACE_ID}:${HERDR_PANE_ID}"
    else
      _HB_CANONICAL_PANE=""
    fi
    if printf '%s' "$_HB_CANONICAL_PANE" | grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
      _HB_STATE_HOME="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
      _HB_HEX_PANE=$(printf '%s' "$_HB_CANONICAL_PANE" | od -An -tx1 | tr -d ' \t\n')
      _HB_PANE_MARKER="$_HB_STATE_HOME/panes/${_HB_HEX_PANE}"
      _HB_VENDOR_ACTIVE="$_HB_STATE_HOME/panes/${_HB_HEX_PANE}.vendor_active"

      _HB_IS_SESSION_TERMINAL=0
      _HB_IS_TURN_TERMINAL=0
      _HB_ARGV_IS_JSON=0
      _HB_ARGV_SID=""

      case "${1:-}" in
        "{"*)
          _HB_ARGV_IS_JSON=1
          _HB_RAW_SID=$(printf '%s' "$1" | grep -m1 -Eo '(\{|,)[[:space:]]*"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)
          if [ -z "$_HB_RAW_SID" ]; then
            _HB_RAW_SID=$(printf '%s' "$1" | grep -m1 -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)
          fi
          if printf '%s' "$_HB_RAW_SID" | grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
            _HB_ARGV_SID="$_HB_RAW_SID"
          fi
          if printf '%s' "$1" | grep -m1 -Eq '(\{|,)[[:space:]]*"(hook_event_name|event|state|type)"[[:space:]]*:[[:space:]]*"(Ended|SessionEnd|session-end)"' 2>/dev/null; then
            _HB_IS_SESSION_TERMINAL=1
          elif printf '%s' "$1" | grep -m1 -Eq '(\{|,)[[:space:]]*"(hook_event_name|event|state|type)"[[:space:]]*:[[:space:]]*"(Stop|Done|AgentDone|AgentWaiting|agent-turn-complete)"' 2>/dev/null; then
            _HB_IS_TURN_TERMINAL=1
          fi
          ;;
        Ended|SessionEnd|session-end) _HB_IS_SESSION_TERMINAL=1 ;;
        Stop|Done|AgentDone|AgentWaiting|agent-turn-complete) _HB_IS_TURN_TERMINAL=1 ;;
      esac

      _HB_GUARD_TMP=""
      _HB_CAPTURE_ERR=0
      if [ "$_HB_IS_SESSION_TERMINAL" -eq 0 ]; then
        if [ ! -t 0 ]; then
          _HB_OLD_UMASK=$(umask)
          umask 077
          mkdir -m 700 -p "$_HB_STATE_HOME" 2>/dev/null || true
          _HB_GUARD_TMP=$(mktemp "$_HB_STATE_HOME/.guard_stdin.XXXXXX" 2>/dev/null || true)
          umask "$_HB_OLD_UMASK"
          if [ -n "$_HB_GUARD_TMP" ]; then
            if command -v perl >/dev/null 2>&1; then
              perl -e '$SIG{ALRM} = sub { exit 142 }; alarm 1; while (sysread(STDIN, my $b, 65536)) { print $b; } alarm 0; exit 0;' > "$_HB_GUARD_TMP" 2>/dev/null || _HB_CAPTURE_ERR=$?
            elif command -v python3 >/dev/null 2>&1; then
              python3 -c 'import sys, signal; signal.signal(signal.SIGALRM, lambda s,f: sys.exit(142)); signal.alarm(1);
while True:
    b = sys.stdin.buffer.read(65536)
    if not b: break
    sys.stdout.buffer.write(b)
signal.alarm(0)' > "$_HB_GUARD_TMP" 2>/dev/null || _HB_CAPTURE_ERR=$?
            else
              # Pure bash cannot safely capture unbounded multiline stdin with timeout.
              # Skip stdin capture and leave stdin untouched for the vendor script.
              rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
              _HB_GUARD_TMP=""
              _HB_CAPTURE_ERR=1
            fi
            if [ "$_HB_CAPTURE_ERR" -ne 0 ]; then
              if [ -s "$_HB_GUARD_TMP" ]; then
                _HB_SPLICE_FIFO=$(mktemp -u "$_HB_STATE_HOME/.guard_splice.XXXXXX" 2>/dev/null || true)
                if [ -n "$_HB_SPLICE_FIFO" ] && mkfifo "$_HB_SPLICE_FIFO" 2>/dev/null; then
                  ( cat "$_HB_GUARD_TMP"; rm -f "$_HB_GUARD_TMP" 2>/dev/null || true; exec cat ) > "$_HB_SPLICE_FIFO" 2>/dev/null &
                  exec < "$_HB_SPLICE_FIFO"
                  rm -f "$_HB_SPLICE_FIFO" 2>/dev/null || true
                else
                  rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
                fi
                _HB_GUARD_TMP=""
              else
                rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
                _HB_GUARD_TMP=""
              fi
            elif [ ! -s "$_HB_GUARD_TMP" ]; then
              rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
              _HB_GUARD_TMP=""
              if [ -z "${1:-}" ]; then
                _HB_CAPTURE_ERR=1
              fi
            fi
          else
            # mktemp failed! Fail open
            _HB_CAPTURE_ERR=1
          fi
        else
          # stdin is a TTY
          if [ -z "${1:-}" ]; then
            _HB_CAPTURE_ERR=1
          fi
        fi
      fi

      _HB_VENDOR_SID="${_HB_ARGV_SID:-}"
      if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
        _HB_RAW_SID=$(grep -m1 -Eo '(\{|,)[[:space:]]*"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$_HB_GUARD_TMP" 2>/dev/null | head -n1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)
        if [ -z "$_HB_RAW_SID" ]; then
          _HB_RAW_SID=$(grep -m1 -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$_HB_GUARD_TMP" 2>/dev/null | head -n1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)
        fi
        if printf '%s' "$_HB_RAW_SID" | grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
          _HB_VENDOR_SID="$_HB_RAW_SID"
        fi
        if grep -m1 -Eq '(\{|,)[[:space:]]*"(hook_event_name|event|state|type)"[[:space:]]*:[[:space:]]*"(Ended|SessionEnd|session-end)"' "$_HB_GUARD_TMP" 2>/dev/null; then
          _HB_IS_SESSION_TERMINAL=1
        elif grep -m1 -Eq '(\{|,)[[:space:]]*"(hook_event_name|event|state|type)"[[:space:]]*:[[:space:]]*"(Stop|Done|AgentDone|AgentWaiting|agent-turn-complete)"' "$_HB_GUARD_TMP" 2>/dev/null; then
          _HB_IS_TURN_TERMINAL=1
        fi
      fi

      _HB_VA_HAS_UUID=0
      if [ -s "$_HB_VENDOR_ACTIVE" ] && grep -m1 -Eq '\{"vendor_session_id":' "$_HB_VENDOR_ACTIVE" 2>/dev/null; then
        _HB_VA_HAS_UUID=1
      fi

      _HB_HERDR_HEALTHY=0
      if [ ! -f "$_HB_STATE_HOME/DISABLED" ] && [ ! -f "$_HB_STATE_HOME/DELIVERY_DOWN" ] && [ -f "$_HB_PANE_MARKER" ] && [ ! -f "${_HB_PANE_MARKER}.failed" ]; then
        _HB_MARKER_MTIME=$(stat -f %m "$_HB_PANE_MARKER" 2>/dev/null || stat -c %Y "$_HB_PANE_MARKER" 2>/dev/null || echo 0)
        _HB_NOW_TIME=$(date +%s)
        if [ $((_HB_NOW_TIME - _HB_MARKER_MTIME)) -lt 60 ] && (pgrep -f "Herdr.app" >/dev/null 2>&1 || pgrep -xi "herdr" >/dev/null 2>&1); then
          _HB_HERDR_HEALTHY=1
        fi
      fi

      if [ "$_HB_IS_SESSION_TERMINAL" -eq 1 ]; then
        rm -f "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
        : # pass through to vendor script
      elif [ -f "$_HB_VENDOR_ACTIVE" ]; then
        # Upgrade bare touch to UUID record if UUID is now available
        if [ "$_HB_VA_HAS_UUID" -eq 0 ] && [ -n "$_HB_VENDOR_SID" ]; then
          _HB_VA_TMP=$(mktemp "$_HB_STATE_HOME/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
          if [ -n "$_HB_VA_TMP" ]; then
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP"
            mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
          else
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
          fi
          _HB_VA_HAS_UUID=1
        fi

        if [ "$_HB_IS_TURN_TERMINAL" -eq 1 ]; then
          : # pass through to vendor script, preserving .vendor_active
        elif [ "$_HB_VA_HAS_UUID" -eq 1 ]; then
          if [ "$_HB_CAPTURE_ERR" -eq 0 ] && [ "$_HB_HERDR_HEALTHY" -eq 1 ]; then
            if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
              rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
            fi
            exit 0
          fi
          if [ -n "$_HB_VENDOR_SID" ]; then
            _HB_VA_TMP=$(mktemp "$_HB_STATE_HOME/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
            if [ -n "$_HB_VA_TMP" ]; then
              printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP"
              mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
            else
              printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
            fi
          fi
        else
          # Bare touch (_HB_VA_HAS_UUID -eq 0)
          if [ "$_HB_IS_TURN_TERMINAL" -eq 1 ]; then
            : # pass through to vendor script
          elif [ "$_HB_CAPTURE_ERR" -eq 0 ] && [ "$_HB_HERDR_HEALTHY" -eq 1 ]; then
            rm -f "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
            if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
              rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
            fi
            exit 0
          fi
          : # Herdr unhealthy: pass through to vendor script
        fi
      elif [ "$_HB_CAPTURE_ERR" -eq 0 ] && [ "$_HB_HERDR_HEALTHY" -eq 1 ]; then
        if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
          rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
        fi
        exit 0
      else
        mkdir -m 700 -p "$_HB_STATE_HOME/panes" 2>/dev/null || true
        if [ -n "$_HB_VENDOR_SID" ]; then
          _HB_VA_TMP=$(mktemp "$_HB_STATE_HOME/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
          if [ -n "$_HB_VA_TMP" ]; then
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP"
            mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
          else
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
          fi
        else
          touch "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
        fi
      fi


      if [ "$_HB_IS_SESSION_TERMINAL" -eq 0 ] && [ -f "$_HB_VENDOR_ACTIVE" ]; then
        touch "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
      fi
      if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
        exec < "$_HB_GUARD_TMP"
        rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
      fi
      unset _HB_CANONICAL_PANE _HB_HEX_PANE _HB_STATE_HOME _HB_PANE_MARKER _HB_VENDOR_ACTIVE \
            _HB_IS_SESSION_TERMINAL _HB_IS_TURN_TERMINAL _HB_VENDOR_SID _HB_VA_HAS_UUID \
            _HB_HERDR_HEALTHY _HB_MARKER_MTIME _HB_NOW_TIME _HB_OLD_UMASK _HB_FIRST_LINE \
            _HB_READ_STATUS _HB_RAW_SID _HB_VA_TMP _HB_CAPTURE_ERR _HB_GUARD_TMP _HB_SPLICE_FIFO \
            _HB_ARGV_IS_JSON _HB_ARGV_SID
    fi
  fi
fi
# END HERDR-BARTENDER DEDUP GUARD```

### 7.3 Hook Installer & Uninstaller (`--install-hooks`, `--uninstall-hooks`)
1. **Hook Installer (`--install-hooks`)**:
   - Scans named vendor hooks: `claude-event-hook.sh` and `codex-notify-hook.sh`.
   - Reads existing permissions `orig_mode = os.stat(hook).st_mode` and preserves existing mode while enforcing executable bits (`target_mode = orig_mode | 0o100`).
   - Computes clean vendor SHA by stripping the guard block between `# BEGIN HERDR-BARTENDER DEDUP GUARD` and `# END HERDR-BARTENDER DEDUP GUARD`. Stores clean SHA in `vendor-hook-sha.json`.
   - **SHA Allowlist Gate for Automatic Repair**: `vendor-hook-sha.json` stores the SHA-256 hashes of known pristine vendor hook scripts. During background reconciliation, automatic hook re-patching checks the unpatched script against `vendor-hook-sha.json`. If an unknown modification or upstream change is detected, automatic patching is blocked, `$STATE_DIR/HOOK_NEEDS_REVIEW` is touched, and manual review is required, preventing unsafe blind patching. The `--status` command checks if `$STATE_DIR/HOOK_NEEDS_REVIEW` exists and displays a prominent warning instructing the user to run `--install-hooks` to approve changes. When `$STATE_DIR/HOOK_NEEDS_REVIEW` is created, the reconciler checks if `$STATE_DIR/.hook_review_alerted` exists; if not, it touches the alerted file and dispatches a macOS alert (`osascript -e 'display notification ...'`). While unpatched, the integration operates in degraded fail-open mode where vendor hooks handle native events directly. Running `--install-hooks` explicitly reviews and re-verifies the hook, unlinking `$STATE_DIR/HOOK_NEEDS_REVIEW`, `$STATE_DIR/.hook_review_alerted`, and `$STATE_DIR/NO_HOOKS`.
   - Saves backup to `${hook}.pristine` with `target_mode = orig_mode | 0o100` permissions if not already present. If `.pristine` exists and clean vendor content changed upstream, updates `.pristine` and stored SHA.
   - Verifies insertion anchor (`set -u` or shebang). If missing, aborts patch safely without touching file.
   - Writes patch to `${hook}.tmp.<pid>`, applies `os.chmod(tmp_path, target_mode)`, verifies syntax with `bash -n`, verifies hook content on disk has not changed during preparation (aborting if concurrently modified), and atomically replaces target via `os.replace`.
2. **Hook Uninstaller (`--uninstall-hooks`)**:
   - Scans named vendor hooks: `claude-event-hook.sh` and `codex-notify-hook.sh`.
   - Reads existing permissions `orig_mode = os.stat(hook).st_mode` and preserves them exactly.
   - Checks if `# BEGIN HERDR-BARTENDER DEDUP GUARD` is present; if absent, logs and skips safely.
   - Strips the delimited guard block cleanly.
   - Verifies hook content on disk has not changed concurrently during preparation.
   - Validates resulting syntax using `bash -n`.
   - Atomically replaces target via `os.replace` preserving `orig_mode`.
   - **Sticky Uninstall Intent**: `--uninstall-hooks` creates `$STATE_DIR/NO_HOOKS` to persistently record the user's intent to keep hooks uninstalled. The background reconciler verifies that `$STATE_DIR/NO_HOOKS` does not exist before performing any automatic hook repair or re-patching. Running `--install-hooks` unlinks `$STATE_DIR/NO_HOOKS`.
   - **Heartbeat Gating on Process Liveness & Active Sessions**: As specified normatively in §5.1 item 5, the background reconciler refreshes marker timestamps every 20s as long as Herdr is alive (`is_herdr_alive()`) for all active non-Ended sessions in cache (including `Idle`, `Done`, `Working`, and `Waiting` within TTL, not salvaged). If Herdr exits (dead), marker refreshing ceases immediately and vendor hooks fall through within 60s.

---

## 8. Security Boundaries, Local Process Liveness Gate & Single-Tenant Scope

- **Single-Tenant Local Scope**: This integration operates strictly as a local macOS IPC bridge between Herdr and Bartender Pro on the same host. Multi-tenant shared servers and remote SSH forwarding are explicitly OUT OF SCOPE for v1.0.
- **Single-Agent-Per-Pane Architecture Scope**: Herdr tracks exactly one agent per terminal pane (`HERDR_PANE_ID`). Deduplication relies on the pane marker. Descendant processes that inherit `HERDR_PANE_ID` (such as nested terminal multiplexers `tmux`/`screen` or nested sub-agents spawned by CLI scripts sharing the pane's environment) are an explicit architectural limitation and out of scope for v1.0. Deduplication applies to the single agent session Herdr tracks for that pane.
- **Local Process Liveness Gate (Best-Effort Defense)**: All outbound event dispatches and health checks to the loopback bridge (`127.0.0.1:7823`) verify that a genuine Bartender process (`Bartender 6` or `Bartender`) is actively running on the host via `get_bartender_pid()`. If Bartender is not running, the request is immediately refused (`False, False`), preventing inadvertent transmission of agent metadata or session titles to unrelated local listeners when Bartender is shut down.
- **Threat Model & Residual Risk**: Under macOS's single-user desktop security model, local processes running under the same user UID share loopback networking. Because Bartender's NotchBar AI Agent bridge exposes an HTTP TCP loopback listener (`127.0.0.1:7823`), cryptographic peer attribution (such as `LOCAL_PEERPID` via `getsockopt` on UNIX domain sockets) is not supported by Bartender's HTTP TCP interface. If malicious software executes under the user's own account and binds port 7823 before Bartender launches, `get_bartender_pid()` acts as an opportunistic liveness gate but cannot cryptographically bind the TCP socket to the PID. Multi-user shared servers and remote SSH port forwarding are explicitly declared OUT OF SCOPE.
- **Literal Loopback Binding**: All HTTP connections strictly target `http://127.0.0.1:${NOTCHBAR_AGENTS_PORT:-7823}`. Connecting strictly uses literal `127.0.0.1` without hostname resolution, completely preventing DNS rebinding vulnerabilities.
- **Port Validation**: Integer in range `1024`–`65535` (default `7823`). Invalid values fall back to `7823` with a warning log.
- **Strict File Permissions**: State directories use `0700` (`rwx------`) and state/orphan files use `0600` (`rw-------`) created under `umask 077`.

---

## 9. Deterministic Rollback & Orphan Replay

### 9.1 Deterministic Rollback Script
To completely remove the integration without orphaned entries or broken hooks:

```bash
#!/bin/bash
set -u

ERRORS=0
STATE_DIR="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
mkdir -p -m 700 "$STATE_DIR"

# Step 0: Create tombstone flag so normal event handlers immediately no-op
touch "$STATE_DIR/DISABLED"

# Step 1: Remove plugin symlinks and verify unlinking
rm -f "$HOME/.config/herdr/plugins/herdr-bartender"
if command -v herdr >/dev/null 2>&1; then
  herdr plugin unlink herdr-bartender 2>/dev/null || true
  if herdr plugin list 2>/dev/null | grep -q "herdr-bartender"; then
    echo "Error: Failed to unlink herdr-bartender plugin from Herdr; keeping tombstone active to prevent events"
    exit 1
  fi
fi

# Step 2: Terminate running background reconcilers and helpers
pkill -9 -f "herdr-bartender --reconcile-background" 2>/dev/null || true

# Step 3: Cleanup of Top Shelf entries (--cleanup explicitly bypasses tombstone and has scalable budget)
CLEANUP_EXIT=0
~/Projects/herdr-bartender/bin/herdr-bartender --cleanup 2>/dev/null || CLEANUP_EXIT=$?
if [ "$CLEANUP_EXIT" -eq 2 ]; then
  echo "Notice: Bartender bridge unreachable during cleanup; active sessions exported to $HOME/.herdr-bartender-orphans.json"
  ERRORS=$((ERRORS + 1))
elif [ "$CLEANUP_EXIT" -ne 0 ]; then
  echo "Warning: Fatal error during cleanup (exit code $CLEANUP_EXIT)"
  ERRORS=$((ERRORS + 1))
fi

# Step 4: Safely strip delimited guard blocks from named vendor hooks
if [ -x ~/Projects/herdr-bartender/bin/herdr-bartender ]; then
  ~/Projects/herdr-bartender/bin/herdr-bartender --uninstall-hooks 2>/dev/null || ERRORS=$((ERRORS + 1))
else
  for hook in "$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/claude-event-hook.sh" \
              "$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/codex-notify-hook.sh"; do
    if [ -f "$hook" ]; then
      BEGIN_COUNT=$(grep -c "# BEGIN HERDR-BARTENDER DEDUP GUARD" "$hook" || true)
      END_COUNT=$(grep -c "# END HERDR-BARTENDER DEDUP GUARD" "$hook" || true)
      if [ "$BEGIN_COUNT" -eq 1 ] && [ "$END_COUNT" -eq 1 ]; then
        python3 -c '
import os, sys, subprocess
h = sys.argv[1]
st = os.stat(h)
with open(h, "r") as f: content = f.read()
before = content.split("# BEGIN HERDR-BARTENDER DEDUP GUARD")[0]
after = content.split("# END HERDR-BARTENDER DEDUP GUARD\n")[-1]
cleaned = before + after.lstrip("\n")
tmp = f"{h}.tmp.{os.getpid()}"
with open(tmp, "w") as f: f.write(cleaned)
os.chmod(tmp, st.st_mode)
if subprocess.run(["bash", "-n", tmp], capture_output=True).returncode == 0:
    os.replace(tmp, h)
else:
    if os.path.exists(tmp): os.unlink(tmp)
    sys.exit(1)
' "$hook" 2>/dev/null || { echo "Error: failed to strip $hook"; ERRORS=$((ERRORS + 1)); }
      elif [ "$BEGIN_COUNT" -gt 0 ] || [ "$END_COUNT" -gt 0 ]; then
        echo "Warning: mismatched dedup markers in $hook; manual inspection required"
        ERRORS=$((ERRORS + 1))
      fi
    fi
  done
fi

# Step 5: Remove plugin state directory ONLY if cleanup confirmed empty (exit code 0) and unlinking succeeded
if [ "$ERRORS" -eq 0 ]; then
  pkill -9 -f "herdr-bartender --reconcile-background" 2>/dev/null || true
  rm -rf "$STATE_DIR"
  echo "Rollback completed successfully."
  exit 0
else
  echo "Rollback finished with $ERRORS warning(s)/error(s). Keeping $STATE_DIR/DISABLED active."
  if [ -f "$HOME/.herdr-bartender-orphans.json" ]; then
    echo "When Bartender 6 is running, replay orphaned sessions via:"
    echo "  ~/Projects/herdr-bartender/bin/herdr-bartender --replay-orphans ~/.herdr-bartender-orphans.json"
  fi
  exit 1
fi
```

### 9.2 Orphan Replay Protocol (`--replay-orphans`)
When `--cleanup` cannot reach Bartender (bridge offline), pending sessions are exported to the canonical orphan file: `$HOME/.herdr-bartender-orphans.json` (outside `$STATE_DIR`, mode `0600`).
Running `./bin/herdr-bartender --replay-orphans <file>`:
1. **File Locking & Ingestion**: Acquires exclusive file lock on `<file>.lock` (`fcntl.flock(lock_fd, fcntl.LOCK_EX)`). Ingests exported session records, bounded to 256 records. Each record preserves `generation` and `admitted_at_ns`.
2. **Schema & Regex Validation**: Validates each `session_id` against `SESSION_ID_REGEX` (`^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$`), skipping invalid entries.
3. **Single Normative Orphan Replay Guard**: `active-sessions.json` is the authoritative state machine; `$HOME/.herdr-bartender-orphans.json` is a durable cold-storage export for terminal/outage recovery. For each orphan record, checks under lock:
   - An orphan record for `session_id` is skipped iff: `active_s = cache.sessions.get(session_id)` (or matching `pane_id`) satisfies `active_s is not None and not active_s.get("salvaged", False) and active_s.get("desired_state") != "Ended"`. When skipped, the orphan is popped/cleared from the orphan file to prevent stale playback.
   - Otherwise (if no active session exists, or the session is `Ended`, or the session is `salvaged`), orphan replay transmits `Ended` to Bartender (with a 0.2s socket timeout). Salvaged sessions (`salvaged == True`) and epoch-dominating salvage generations (`>= 1_700_000_000`) NEVER satisfy the skip predicate and strictly never suppress orphan replays.
4. Posts `{"state": "Ended", "agent": s.get("agent", "Herdr"), "session_id": sid}` to `/event`.
4a. **Post-Send Re-Sync**: Immediately following HTTP 200 delivery of `Ended` for an orphan, re-acquires cache lock. If a fresh active session was admitted while the orphan send was in flight, forces re-sync (`delivered_seq = 0, delivery_status = "in_flight"`) and touches `reconciler.pending`.
5. If response is 200 OK (confirmed Ended on bridge), removes record from the orphan file and clears marker. If rejected (4xx) or unreachable (network failure/5xx), automatically retries with minimal payload; if still unconfirmed, the record is **retained** in the orphan file to prevent data loss.
6. When all records are cleared, unlinks the orphan file. If any records remain undelivered, atomically rewrites remaining records via temporary file under `umask 077` (mode `0600`).
7. **Orphan File Isolation**: Orphan replay operates exclusively on the designated orphan file and unlinks it once all records are confirmed delivered. Orphan replay NEVER removes `$STATE_DIR`, never alters active sessions cache records unless confirming delivery, and never unlinks `$STATE_DIR/DISABLED` (destruction of `$STATE_DIR` and unlinking of `DISABLED` is strictly reserved for the deterministic rollback script in §9.1).

---

## 10. Automated Test Plan & Multi-Process Verification

### 10.1 Automated Unit & Multi-Process Suite (`--unit-test`)
The test suite running against an ephemeral mock HTTP server verifies all 60 core invariants:
1. **Mock Bridge Contract & Liveness Check**: Verifies `/health` returns `{"ok":true,"port":...}`.
2. **Lifecycle State Transitions**: Sequence of `working` &rarr; `blocked` (`Waiting`) &rarr; `done` &rarr; `idle`.
3. **Container Cascades with Lease Integration**: Verifies `pane.closed`, `tab.closed`, and `workspace.closed` end matching sessions and respect active leases.
4. **Qualification & Agent Exit**: Verifies unassisted shell panes are ignored, and agent exit (`agent: null` / `""`) transitions to `Ended`.
5. **Debounced `unknown` Status**: Asserts `unknown` status does not evict active session.
6. **Active Supersession in Drain Loop**: Simulates `Ended` in-flight while a new `Working` event arrives; asserts session is NOT deleted from cache and `Working` is transmitted to bridge.
7. **Monotonic Integer Counter**: Verifies integer `seq` increments monotonically across all events, and older source timestamps are dropped.
8. **Response Code Matrix & Dedup Guard Fall-Through**: Asserts HTTP 200/ok:false, 3xx, and 4xx mark `delivery_status = "non_retryable_failed"`, touch `<hex>.failed`, and verify vendor hook guard falls through immediately.
9. **Hex-Encoded Pane Marker Lifecycle & Freshness**: Verifies hex marker is created on confirmed delivery, removed on pane closure, and refreshed by watchdog heartbeat.
10. **Atomic Spool Directory & Chronological Ordering**: Injects spool envelopes with varied timestamps; asserts FIFO ingestion and monotonic supersession.
11. **Helper Singleton with Pending Flag & Fixed Backoff**: Verifies `--reconcile-background` detects `$STATE_DIR/reconciler.pending` and loops to complete newly arrived work before exiting.
12. **Signal Safety & Non-Destructive Quarantine**: Asserts malformed cache is quarantined to `.corrupt.<timestamp>`, candidate IDs salvaged with `seq == delivered_seq` without marking Ended, and excluded from automatic transmission.
13. **Bounded Timeout Enforcement**: Asserts process exits within 1.5s when mock bridge delays responses.
14. **Tombstone DISABLED Flag**: Asserts plugin immediately exits 0 when `$STATE_DIR/DISABLED` exists, while `--cleanup` successfully executes.
15. **Bounded TTLs for Working & Waiting Sessions**: Asserts `Working` sessions expire after 12h, and `Waiting` sessions expire after 48h.
16. **Orphan Replay (`--replay-orphans`)**: Verifies exported orphan cache is replayed and cleared when Bartender bridge returns online.
17. **Delivery Down Marker & Fast Fall-Through**: Asserts first delivery failure touches `<hex>.failed`, and 3 consecutive failures set `DELIVERY_DOWN`.
18. **Background Pane Context Isolation**: Asserts background pane events do not inherit focused pane `cwd`, `tab_id`, or `agent`.
19. **Vendor Fall-Through Tracking, Bare-Touch Retention & Lifecycle Cleanup**: Asserts `.vendor_active` marker is created on fall-through; asserts `remove_pane_marker` cleans up pane markers without touching `.vendor_active`; asserts `cleanup_vendor_active` unlinks bare touches (no UUID) on confirmed delivery to restore dedup while dismissing UUID-bearing vendor files via bridge Ended event.
20. **Reconciler Quiet Outage Health Recovery**: Asserts `retryable_exhausted` session and `DELIVERY_DOWN` are re-armed and delivered upon bridge health recovery without follow-up events.
21. **Bartender PID Change Triggers Full Re-Sync**: Asserts bridge restart resyncs active sessions.
22. **Hook Installation Preserves Executable Mode**: Asserts patched hooks retain `orig_mode | 0o100` executable permissions.
23. **Hook Guard Terminal Unlinking and Healthy Pane Suppression**: Asserts both argv (`$1=Ended`) and stdin JSON (`hook_event_name: "SessionEnd"`) cleanly remove `.vendor_active` without re-touching it, while turn-terminal events (`Stop`, `Done`) retain `.vendor_active`, and asserts healthy pane suppresses turn-terminal vendor events (`Stop`, `Done`) when `.vendor_active` is absent (session-terminal events `Ended`/`SessionEnd` pass through unconditionally).
24. **Post-Close Tombstone Table Rejection**: Asserts late-arriving status events (`arrival_ns <= tombstone_ns`) are rejected and cannot resurrect closed panes.
25. **Cleanup Exit Code Contract & Canonical Permissions**: Asserts `--cleanup` returns 0 when bridge reachable, returns 2 and exports to `$HOME/.herdr-bartender-orphans.json` with mode `0600` when bridge unreachable.
26. **Context Isolation with None/Missing `focused_pane_id`**: Asserts events when `focused_pane_id` is None or omitted strictly evaluate `is_focused = False` and do not leak `cwd` or `tab_id`.
27. **Multi-Process Close vs Status Race with Positive Admission Check**: Asserts events arriving within 60s of close without positive agent metadata are rejected and preserve tombstone; events with positive agent metadata pop tombstone and admit new session.
28. **Spool Poison-Pill Quarantine to `spool/bad/` and FIFO Continuity**: Asserts corrupt or malformed JSON envelope in `spool/` is quarantined to `spool/bad/` and unblocks subsequent valid envelopes.
29. **Ended Retry with Minimal Payload and Retention on Non-200**: Asserts Ended payload rejection automatically retries with minimal payload `{state, agent, session_id}`; asserts un-clearable sessions are retained in cache and exported to orphan file rather than evicted.
30. **Stranded Vendor Entry Dismissal via `cleanup_vendor_active` Outside Lock**: Asserts confirmed Herdr delivery for a pane with `.vendor_active` stages vendor dismissal under lock and dispatches bridge Ended event outside the file lock (`assert not IN_CRITICAL_SECTION`), unlinking `.vendor_active`.
31. **Lowercase Hex Encoding Parity between Python and Bash**: Verifies `canonical_pane.encode('utf-8').hex()` exactly equals `printf '%s' "$CANONICAL_PANE" | od -An -tx1 | tr -d ' \t\n'`.
32. **Byte-Exact Stdin Spooling & Integrity in Hook Guard**: Verifies >64KB input payloads are captured byte-exact without shell truncation or data loss.
33. **Turn-Terminal vs Session-Terminal Hook Guard Distinctions**: Asserts `Stop`, `Done`, `AgentDone`, and `AgentWaiting` pass through to vendor while retaining `.vendor_active`, whereas `Ended` and `SessionEnd` pass through and unlink `.vendor_active`.
34. **Canonical Lease Takeover Truth Table Verification (All 6 Rows with PID Reuse Defense)**: Exhaustively asserts all 6 takeover conditions: (Row 1) holder is None (unclaimed) -> CLAIM; (Row 2) caller is holder_pid (re-entrant) -> CLAIM; (Row 3) holder PID is dead or start time changed -> CLAIM immediately; (Row 4) holder PID is alive with matching start time and now < deadline -> DEFER; (Row 5) holder PID is alive with matching start time and deadline <= now < deadline + 0.5s -> DEFER; (Row 6) holder PID is alive with matching start time and now >= deadline + 0.5s -> CLAIM (hung takeover).
35. **Monotonic Generation Ordering under Backward Wall-Clock Adjustment**: Simulates wall-clock step backward; asserts generation counters strictly advance monotonically and spooled events from older generations are dropped.
36. **Orphan Replay Generation Guard**: Asserts replay skips sending `Ended` for panes where a newer generation exists in active cache.
37. **Canonical Parity Fixture Table across Python and Bash**: Evaluates exhaustive matrix of raw pane ID and workspace ID inputs through both Python `normalize_pane_id` and Bash canonicalization; asserts exact byte-for-byte parity across all variations, and asserts colon-less pane IDs without workspace_id are rejected.
38. **Persistent Generation Counter Surviving Cache Eviction on Ended**: Asserts `pane_generations` persists at the root across session eviction on confirmed `Ended`, and subsequent turn on the same pane strictly increments the persistent generation counter.
39. **Stale Agent-Exit Dropped by Arrival Ordering**: Asserts agent exit event arriving with arrival timestamp older than the current session's `admitted_at_ns` is safely dropped without ending the session.
40. **Tombstone Source Timestamp Rejection**: Asserts status events carrying `event.data.timestamp <= last_source_timestamp` recorded in a pane tombstone are strictly rejected.
41. **Fail-Open Stdin Capture under mktemp Failure**: Simulates failure of `mktemp` in hook guard; asserts guard fails open without truncating stdin, unlinking, or raising error.
42. **Step C Lease Mismatch Forces Re-Sync**: Asserts that when Step C encounters a lease token superseded by another process, it forces a re-sync (`delivered_seq = 0, delivery_status = "in_flight"`) and signals the background reconciler.
43. **Stale Send Compensation on Evicted Session**: Verifies that if an event successfully completes HTTP delivery but the session was concurrently evicted or tombstoned by a cascade, sender immediately compensates by posting `Ended` to Bartender Pro to dismiss the resurrected phantom session.
44. **Cross-Workspace Same Raw ID Isolation**: Verifies that two distinct workspaces containing the same raw pane ID (e.g. `w1:p1` and `w2:p1`) produce distinct canonical IDs, distinct injective hex markers, and distinct lock-isolated states without cross-workspace marker collision.
45. **Bare-Touch `.vendor_active` Preservation & Pane-Close Cleanup**: Asserts bare-touch `.vendor_active` is preserved past 60s during active session (no blind expiry) and cleaned up on pane close (`is_pane_closed=True`).
46. **Bounded Stdin Capture Fail-Open on Never-Closing FIFO**: Asserts hook guard bounds wait time to <=1.0s and fails open without hanging or truncating when piped to an unclosed FIFO or open pipe.
47. **Automatic Orphan Replay in Reconciler Loop**: Asserts background reconciler automatically checks for `$HOME/.herdr-bartender-orphans.json` and replays orphaned sessions when bridge `/health` succeeds.
48. **Stale Send Compensation Aborted if Active Session Re-admitted**: Asserts that under-lock re-verification aborts compensating `Ended` dispatch if a fresh turn has already re-admitted the session.
49. **Global `next_generation` Root Counter Survives Eviction**: Asserts that `next_generation` at cache root survives eviction of inactive entries from `pane_generations`, preserving strict generation monotonicity.
50. **Corrupt Cache Salvage Preserves Close Envelopes & Quiescent Model**: Asserts that corrupt cache recovery strictly preserves close envelopes in `spool/`, quarantines status envelopes to `spool/bad/`, and stages recovered sessions quiescently without phantom Top Shelf state.
51. **256 Active Session Capacity Protection**: Asserts that when cache reaches 256 active running sessions, admission of session 257 is rejected with warning, protecting against unbounded cache growth.
52. **Vendor Session-Terminal Pass-Through without `.vendor_active`**: Asserts that when `.vendor_active` is absent and Herdr is healthy, passing `Ended` or `SessionEnd` to the hook guard does not exit 0, but passes through to the vendor script so any late-landing or raced vendor Top Shelf entries are cleanly dismissed.
53. **Earliest-Start-Time Herdr PID Selection**: Asserts that when multiple PIDs match `herdr`, `get_herdr_pid()` inspects process start times (`ps -p <pid> -o lstart=`) and deterministically selects the original GUI app instance, remaining immune to transient CLI invocations (`herdr plugin list`) and PID wraparound.
54. **Persisted Compensation and Vendor Cleanup Drained in Reconciler Loop**: Asserts that any pending compensations or vendor cleanups recorded in `active-sessions.json` under lock are drained by the background reconciler with under-lock re-verification, aborting if a fresh session was re-admitted and forcing re-sync if raced during send.
55. **Vendor Hook Pass-Through under Bare-Touch `.vendor_active` (Bounded Vendor Dismissal Guarantee)**: Asserts that when `.vendor_active` is a bare touch (`_VA_HAS_UUID == 0`), turn-terminal (`Stop`, `Done`, `AgentDone`, `AgentWaiting`) events pass through to the vendor hook, while non-terminal events under healthy Herdr suppress (`exit 0`) and unlink the bare touch to restore dedup.
56. **Bounded Hook Guard Stdin Capture on Stalled FIFO Pipe with Replay-plus-Remainder Splice**: Asserts hook guard captures stdin within strictly <=1.0s (unbounded payload size), uses unlinked temporary FIFO written by background subshell to splice captured prefix with remainder, and fails open without hanging on exit or losing input on slow/open pipes (<2.0s).
57. **Marker Heartbeat for Idle and Done Sessions**: Asserts reconciler watchdog sweeps and refreshes pane markers for active non-Ended sessions in `Idle` and `Done` states (in addition to `Working` and `Waiting`) while Herdr is alive, preventing premature vendor hook fall-through after 60s idle between turns.
58. **Corrupt Cache Salvage Preserves Orphan Replay with Epoch Generations**: Asserts that when corrupt cache recovery assigns an epoch generation (`>= 1_700_000_000`), orphan replay guard strictly distinguishes between active live sessions and salvaged/epoch states, successfully replaying and clearing orphaned sessions.
59. **Agent Process Exit Shell Isolation vs Container Tombstones**: Asserts agent process termination in an open shell (`pane.agent_status_changed` with empty agent) evicts the agent session without recording a container tombstone; records in `agent_exits` table to drop stale events predating exit; subsequent agent startup on the open pane is admitted even if source timestamp is omitted and clears `agent_exits` entry.
60. **Dismissal Queue Cancellation on Vendor Fallback**: Asserts that if a vendor fallback occurs (`.vendor_active` created, `.failed` touched, or Herdr dies) while a vendor UUID dismissal is queued in `dismissed_vendor_uuids`, the queued dismissal is immediately cancelled and purged without sending `Ended`, preventing the fallback vendor session from being killed.
61. **Codex CLI Argv JSON Payload Extraction in Hook Guard**: Asserts hook guard extracts `session_id` and detects turn-terminal (`agent-turn-complete`), session-terminal (`session-end`), and non-terminal events from `$1` JSON payloads, correctly passing through terminal events and unlinking bare touches on session-terminal events.
62. **Fail-Open on Empty TTY or Pipe without Argv Token/JSON**: Asserts hook guard immediately fails open (`PASSTHROUGH`) to vendor hook when stdin is empty (TTY or closed pipe) and no command-line token or JSON payload is passed in `$1`.
63. **Hung Sender Lease Takeover Race Defeated by `resync_generation`**: Asserts that when a hung sender returns after lease takeover and detects a lease token mismatch, it increments `resync_generation` and resets `delivered_seq = 0`; the superseding sender checks `resync_generation` on return and forces an in-flight re-sync if the generation advanced during its send.
64. **Reconciler Ended Confirmation Preserving Step A Origins**: Asserts that when the background reconciler confirms delivery of an Ended event, it creates container tombstones using persisted Step A origins (`closed_at_ns`, `closed_source_ts`, `last_source_timestamp`) directly from the session record, remaining completely independent of unpersisted in-memory event data or `arr_ns`.
65. **Reconciler Settling Window Retries for `dismissed_vendor_uuids`**: Asserts that the background reconciler retains entries in `dismissed_vendor_uuids` across a 10-second settling window, retrying every 2.0s, incrementing `attempts`, and purging only after 10s have elapsed or 5 attempts have been made.
66. **Normative `STATE_DIR` Resolution Parity Across Python and Bash**: Asserts identical state directory resolution between Python `get_state_dir()` and bash `${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}` across all precedence configurations (custom state dir set, XDG default, fallback to `$HOME/.local/state`).
67. **Turn-Terminal Pass-Through under UUID-Bearing `.vendor_active`**: Asserts that when `.vendor_active` holds a UUID record, turn-terminal events (`Stop`, `Done`, `AgentDone`, `AgentWaiting`, `agent-turn-complete`) pass through to the vendor hook under healthy Herdr while preserving `.vendor_active`.
68. **Active Notification and Degraded Mode on `HOOK_NEEDS_REVIEW`**: Asserts reconciler touches `.hook_review_alerted` and dispatches macOS notification when `HOOK_NEEDS_REVIEW` is created; asserts `install_hooks` unlinks both flags and restores normal operation.

### 10.2 Clock Injection & Multi-Process Test Harness
- **Injectable Clock Interface**: TTL (12h, 48h) and absence horizon (12h) tests control time by injecting simulated epoch timestamps into session `last_event_at`, manipulating file mtimes via `os.utime`, and monkeypatching `time.time` and `time.time_ns` during unit testing, avoiding long real-time waits while verifying exact expiration boundaries.
- **Live Acceptance Checklist (Manual/Automated against Live Software)**:
  1. *Bartender 6.0.4 on macOS*: Start Bartender 6 with Top Shelf enabled. Run `./bin/herdr-bartender --live-test`. Verify `POST /event` displays icons in NotchBar Top Shelf, `state: Ended` dismisses entries, and `/health` decrements session count to 0.
  2. *Herdr 0.8.x Integration*: Symlink plugin into `~/.config/herdr/plugins/herdr-bartender`. Start Herdr. Open Claude Code session in pane. Verify status transitions reflect in NotchBar.
  3. *Vendor Handover Test*: Run `claude` command. Terminate Herdr (`killall herdr`). Verify vendor hook falls through to notify Bartender under native UUID. Restart Herdr; verify `cleanup_vendor_active` dismisses vendor entry and restores Herdr ownership.

---

## 11. Normative Fixtures

### 11.1 Herdr Event Payloads (Herdr 0.8.x)

```json
// Event: pane.agent_status_changed (blocked -> Waiting)
{
  "event": "pane.agent_status_changed",
  "data": {
    "pane_id": "w1:p1",
    "workspace_id": "w1",
    "tab_id": "w1:t1",
    "agent": "claude",
    "agent_status": "blocked",
    "title": "Reviewing pull request #42",
    "timestamp": 1727998410.12
  }
}
```

```json
// Event: pane.agent_status_changed (active agent idle at prompt)
{
  "event": "pane.agent_status_changed",
  "data": {
    "pane_id": "w1:p1",
    "workspace_id": "w1",
    "tab_id": "w1:t1",
    "agent": "claude",
    "agent_status": "idle",
    "timestamp": 1727998420.00
  }
}
```

```json
// Event: pane.agent_status_changed (agent exited shell -> Ended)
{
  "event": "pane.agent_status_changed",
  "data": {
    "pane_id": "w1:p1",
    "workspace_id": "w1",
    "tab_id": "w1:t1",
    "agent": null,
    "agent_status": "idle",
    "timestamp": 1727998430.00
  }
}
```

```json
// Event: pane.closed
{
  "event": "pane.closed",
  "data": {
    "pane_id": "w1:p1",
    "workspace_id": "w1"
  }
}
```

```json
// Event: tab.closed
{
  "event": "tab.closed",
  "data": {
    "tab_id": "w1:t1",
    "workspace_id": "w1"
  }
}
```

```json
// Event: workspace.closed
{
  "event": "workspace.closed",
  "data": {
    "workspace_id": "w1"
  }
}
```

### 11.2 Bartender Pro Bridge Contracts (Bartender 6.0.4 on macOS)

```json
// POST /event Request
{
  "state": "Waiting",
  "agent": "Claude (Herdr)",
  "session_id": "herdr:macbook:w1:p1",
  "title": "Reviewing pull request #42",
  "cwd": "/Users/voodootikigod/Projects/app",
  "terminal": "Herdr",
  "event": "pane.agent_status_changed",
  "seq": 4
}

// POST /event Response (Success - verified from live Bartender 6.0.4)
{
  "ok": true
}

// GET /health Response (Ready - verified from live Bartender 6.0.4)
{
  "ok": true,
  "port": 7823
}
```

### 11.3 Vendor Hook Payloads (Captured from Official Hook Scripts)

```json
// Claude Event Hook input ($HOOK_JSON to stdin)
{
  "session_id": "a82e9232-0d36-43e6-8b02-1b953babd13e",
  "hook_event_name": "UserPromptSubmit",
  "cwd": "/Users/voodootikigod/Projects/app",
  "prompt": "Fix authentication bug",
  "is_interrupt": false
}

// Codex Notify Hook input ($HOOK_JSON to stdin)
{
  "session_id": "c91f0443-1e47-54f7-9c13-2c064cbee24f",
  "hook_event_name": "AgentWaiting",
  "cwd": "/Users/voodootikigod/Projects/app",
  "prompt": "Review tests"
}

// Codex CLI argv JSON invocation ($1)
// codex-notify-hook.sh '{"session_id":"c91f0443-1e47-54f7-9c13-2c064cbee24f","hook_event_name":"AgentWaiting","cwd":"/workspace"}'
```
