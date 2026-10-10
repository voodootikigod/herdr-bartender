#!/bin/bash
set -u

# Drain stdin completely to prevent EPIPE to writing agent processes
RAW_INPUT="$(cat 2>/dev/null || true)"

# Resolve EVENT from AGY_HOOK_EVENT, argv $1, or top-level hook_event_name in JSON
EVENT="${AGY_HOOK_EVENT:-${1:-}}"
if [ -z "$EVENT" ]; then
  EVENT=$(printf '%s' "$RAW_INPUT" | LC_ALL=C grep -o '"hook_event_name"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
fi

# Whitelist EVENT strictly to supported lifecycle status events
case "$EVENT" in
  PreInvocation|PostInvocation|Stop|SessionEnd) ;;
  *) exit 0 ;;
esac

# Strictly loopback only (Plan §8: literal loopback, no hostname resolution, no remote proxy)
HOST="127.0.0.1"
PORT="${NOTCHBAR_AGENTS_PORT:-7823}"
if ! printf '%s' "$PORT" | grep -Eq '^[0-9]{1,5}$' || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  PORT=7823
fi

# Global request counter to cap total network requests per invocation (well under 5s timeout)
TOTAL_NETWORK_REQUESTS=0
MAX_NETWORK_REQUESTS=6

# Record invocation start for hard deadline checking (well under 5s hook timeout)
START_TIME=$(date +%s 2>/dev/null || echo 0)
has_time_remaining() {
  [ "$TOTAL_NETWORK_REQUESTS" -ge "$MAX_NETWORK_REQUESTS" ] && return 1
  [ "$START_TIME" -eq 0 ] && return 0
  local now
  now=$(date +%s 2>/dev/null || echo 0)
  [ $(( now - START_TIME )) -lt 3 ]
}

# Cleanup trap for temporary scratch files
TMP_VA=""
cleanup_temp_files() {
  [ -n "${TMP_VA:-}" ] && rm -f "$TMP_VA" 2>/dev/null || true
}
trap cleanup_temp_files EXIT INT TERM

# Check if Bartender 6 / Bartender is running (memoised per invocation)
BARTENDER_ALIVE_STATUS=""
BARTENDER_UNREACHABLE=0

is_bartender_alive() {
  [ "$BARTENDER_UNREACHABLE" -eq 1 ] && return 1
  if [ -z "$BARTENDER_ALIVE_STATUS" ]; then
    if pgrep -xi "Bartender 6" >/dev/null 2>&1 \
      || pgrep -xi "Bartender" >/dev/null 2>&1 \
      || pgrep -f '^[^[:space:]]*/Bartender( 6)?\.app/Contents/MacOS/' >/dev/null 2>&1; then
      BARTENDER_ALIVE_STATUS="1"
    else
      BARTENDER_ALIVE_STATUS="0"
    fi
  fi
  [ "$BARTENDER_ALIVE_STATUS" = "1" ]
}

# Dismiss a session ID directly via Bartender HTTP endpoint.
# Returns 0 on successful delivery (HTTP 200) or permanent resolution (HTTP 404, 410).
# Returns 1 on delivery failure or transient errors (e.g. 429, 500, network error).
send_dismissal() {
  local target_sid="$1"
  [ -n "$target_sid" ] || return 1
  has_time_remaining || return 1
  is_bartender_alive || return 1
  TOTAL_NETWORK_REQUESTS=$((TOTAL_NETWORK_REQUESTS + 1))
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    --noproxy '*' \
    --max-redirs 0 \
    --proto =http \
    --connect-timeout 0.15 \
    --max-time 0.5 \
    -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "{\"state\":\"Ended\",\"agent\":\"Antigravity\",\"session_id\":\"${target_sid}\"}" 2>/dev/null || true)
  case "$code" in
    200|404|410) return 0 ;;
    000*|"")
      # Connection failure or timeout: short-circuit all later requests in this invocation
      BARTENDER_UNREACHABLE=1
      return 1
      ;;
    *) return 1 ;;
  esac
}

# Resolve canonical pane ID and paths if HERDR_PANE_ID is set
CANONICAL_PANE=""
HEX_PANE=""
STATE_HOME="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/herdr/plugins/herdr-bartender}"
PANE_MARKER=""
VENDOR_ACTIVE=""

if [ -n "${HERDR_PANE_ID:-}" ] && printf '%s' "$HERDR_PANE_ID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
  if printf '%s' "$HERDR_PANE_ID" | grep -q ':'; then
    CANONICAL_PANE="$HERDR_PANE_ID"
  elif [ -n "${HERDR_WORKSPACE_ID:-}" ]; then
    CANONICAL_PANE="${HERDR_WORKSPACE_ID}:${HERDR_PANE_ID}"
  fi
  if [ -n "$CANONICAL_PANE" ] && printf '%s' "$CANONICAL_PANE" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
    HEX_PANE=$(printf '%s' "$CANONICAL_PANE" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n')
    PANE_MARKER="${STATE_HOME}/panes/${HEX_PANE}"
    VENDOR_ACTIVE="${STATE_HOME}/panes/${HEX_PANE}.vendor_active"
  fi
fi

# Find agent PID: match ONLY binary basename (agy, antigravity, Antigravity),
# NEVER matching wrapper shells (sh, bash, zsh) whose arguments may contain the script path.
find_agent_pid() {
  local pid=$PPID
  local max=8
  while [ "$max" -gt 0 ] && [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    local comm base_comm
    comm=$(ps -o comm= -p "$pid" 2>/dev/null || true)
    base_comm=$(basename "$comm" 2>/dev/null || echo "$comm")
    case "$base_comm" in
      agy|antigravity|Antigravity) echo "$pid"; return 0 ;;
    esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    max=$((max - 1))
  done
  echo ""
  return 0
}

AGENT_PID="${AGY_HOOK_AGENT_PID:-${AGENT_PID:-$(find_agent_pid 2>/dev/null || true)}}"
if ! printf '%s' "${AGENT_PID:-}" | grep -Eq '^[0-9]{1,9}$'; then
  AGENT_PID=""
fi

# Resolve controlling terminal for multi-session collision avoidance
if [ "${AGY_HOOK_NO_TTY:-0}" = "1" ] || [ "${AGENT_TTY:-}" = "none" ]; then
  AGENT_TTY=""
else
  AGENT_TTY="${AGY_HOOK_AGENT_TTY:-${AGENT_TTY:-}}"
  if [ -z "$AGENT_TTY" ] || [ "$AGENT_TTY" = "?" ] || [ "$AGENT_TTY" = "??" ]; then
    if [ -n "$AGENT_PID" ]; then
      AGENT_TTY=$(ps -o tty= -p "$AGENT_PID" 2>/dev/null | tr -d ' \t\n' || true)
    fi
    if [ -z "$AGENT_TTY" ] || [ "$AGENT_TTY" = "?" ] || [ "$AGENT_TTY" = "??" ]; then
      AGENT_TTY=$(ps -o tty= -p $$ 2>/dev/null | tr -d ' \t\n' || true)
    fi
    if [ "$AGENT_TTY" = "?" ] || [ "$AGENT_TTY" = "??" ]; then
      AGENT_TTY=""
    fi
  fi
fi

# Validate AGENT_TTY strictly to prevent JSON / path injection
if ! printf '%s' "${AGENT_TTY:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
  AGENT_TTY=""
fi

# Check if Herdr is demonstrably healthy and actively owns this pane.
is_herdr_owning_pane() {
  [ -n "$CANONICAL_PANE" ] || return 1
  [ -n "$PANE_MARKER" ] || return 1

  # Flag files: if disabled or delivery down, do not suppress
  [ -e "${STATE_HOME}/DISABLED" ] && return 1
  [ -L "${STATE_HOME}/DISABLED" ] && return 1
  [ -e "${STATE_HOME}/DELIVERY_DOWN" ] && return 1
  [ -L "${STATE_HOME}/DELIVERY_DOWN" ] && return 1

  # Marker must exist as a real regular file and not be failed
  [ -f "$PANE_MARKER" ] && [ ! -L "$PANE_MARKER" ] || return 1
  [ -e "${PANE_MARKER}.failed" ] && return 1
  [ -L "${PANE_MARKER}.failed" ] && return 1

  # Marker freshness: mtime within 60s
  local mtime now age
  mtime=$(stat -c %Y "$PANE_MARKER" 2>/dev/null || stat -f %m "$PANE_MARKER" 2>/dev/null || echo 0)
  now=$(date +%s 2>/dev/null || echo 0)
  age=$(( now - mtime ))
  [ "$age" -ge 0 ] && [ "$age" -lt 60 ] || return 1

  # Herdr process must be alive
  if pgrep -a -xi "herdr" >/dev/null 2>&1 || pgrep -a -f '^[^[:space:]]*/Herdr\.app/Contents/MacOS/' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Safely extract all valid vendor session IDs from a record file.
# Avoids matching JSON keys (like vendor_session_id or pending_dismissal_sid)
# and properly splits space- and comma-separated tokens.
extract_record_sids() {
  local target_file="$1"
  [ -f "$target_file" ] || return 0
  local content
  content=$(cat "$target_file" 2>/dev/null || true)
  [ -n "$content" ] || return 0
  local v_sid p_sid arr_sids
  v_sid=$(printf '%s' "$content" | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
  p_sid=$(printf '%s' "$content" | LC_ALL=C grep -o '"pending_dismissal_sid"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
  arr_sids=$(printf '%s' "$content" | LC_ALL=C grep -o '"pending_dismissal_sid"[[:space:]]*:[[:space:]]*\[[^]]*\]' 2>/dev/null | LC_ALL=C grep -o '"[a-zA-Z0-9_-]\{16,64\}"' 2>/dev/null | tr -d '"' || true)

  local raw_tokens
  raw_tokens=$(printf '%s %s %s' "$v_sid" "$p_sid" "$arr_sids" | tr ',' ' ')

  set -f
  local extracted=""
  for token in $raw_tokens; do
    [ -n "$token" ] || continue
    [ "$token" = "vendor_session_id" ] && continue
    [ "$token" = "pending_dismissal_sid" ] && continue
    if printf '%s' "$token" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
      case " $extracted " in
        *" $token "*) ;;
        *) extracted="${extracted:+${extracted} }${token}" ;;
      esac
    fi
  done
  set +f
  printf '%s' "$extracted"
}

# Safely claim and retire a .vendor_active file (mirroring vendor.retire_vendor_file).
# Checks that the claimed file's SIDs are within the set of confirmed_sids;
# if unconfirmed or newer SIDs exist, restores/merges them so they are never lost.
retire_vendor_file() {
  local target="$1"
  local confirmed_sids="${2:-}"
  [ -f "$target" ] || return 0
  local t_dir
  t_dir=$(dirname "$target")
  local claim="${target}.claim-$$-$(date +%s%N 2>/dev/null || date +%s 2>/dev/null || echo $$)"
  if mv -f "$target" "$claim" 2>/dev/null; then
    if [ -z "$confirmed_sids" ]; then
      # Unconditional retirement (e.g. swept dead process)
      rm -f "$claim" 2>/dev/null || true
      return 0
    fi

    local claimed_sids
    claimed_sids=$(extract_record_sids "$claim")
    if [ -z "$claimed_sids" ]; then
      # Empty or invalid record
      rm -f "$claim" 2>/dev/null || true
      return 0
    fi

    # Check for any SIDs in $claim that were NOT confirmed dismissed
    local unconfirmed_sids=""
    set -f
    for s in $claimed_sids; do
      case " $confirmed_sids " in
        *" $s "*) ;;
        *) unconfirmed_sids="${unconfirmed_sids:+${unconfirmed_sids} }${s}" ;;
      esac
    done
    set +f

    if [ -z "$unconfirmed_sids" ]; then
      # All SIDs in claimed record were confirmed dismissed
      rm -f "$claim" 2>/dev/null || true
    else
      # Some SIDs in $claim were not confirmed dismissed (concurrent write or un-attempted).
      # Rewrite $claim with remaining unconfirmed SIDs and merge-and-commit into $target.
      local first_u="" rest_u=""
      for s in $unconfirmed_sids; do
        if [ -z "$first_u" ]; then
          first_u="$s"
        else
          rest_u="${rest_u:+${rest_u} }${s}"
        fi
      done
      local c_pid c_tty c_pid_json="" c_tty_json=""
      c_pid=$(cat "$claim" 2>/dev/null | LC_ALL=C grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' 2>/dev/null | head -n1 | grep -o '[0-9]*' || true)
      c_tty=$(cat "$claim" 2>/dev/null | LC_ALL=C grep -o '"tty"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
      if printf '%s' "${c_pid:-}" | grep -Eq '^[0-9]{1,9}$'; then
        c_pid_json=",\"pid\":${c_pid}"
      fi
      if printf '%s' "${c_tty:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
        c_tty_json=",\"tty\":\"${c_tty}\""
      fi
      if [ -z "$rest_u" ]; then
        printf '{"vendor_session_id":"%s"%s%s}\n' "$first_u" "$c_pid_json" "$c_tty_json" > "$claim" 2>/dev/null || true
      else
        printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s%s}\n' "$first_u" "$rest_u" "$c_pid_json" "$c_tty_json" > "$claim" 2>/dev/null || true
      fi
      merge_and_commit_vendor_file "$claim" "$target" "$confirmed_sids"
    fi
  fi
}

# Atomically merge temporary vendor record into target .vendor_active, preserving concurrent SIDs and PID/TTY.
# Optional $3 provides confirmed_sids to exclude from the merged output.
merge_and_commit_vendor_file() {
  local tmp_file="$1"
  local target="$2"
  local exclude_sids="${3:-}"
  [ -f "$tmp_file" ] || return 0
  [ -n "$target" ] || { rm -f "$tmp_file" 2>/dev/null || true; return 0; }

  local t_dir
  t_dir=$(dirname "$target")
  mkdir -m 700 -p "$t_dir" 2>/dev/null || true

  if [ ! -f "$target" ]; then
    if [ -n "$exclude_sids" ]; then
      local t_sids first_t="" rest_t="" rest_t_count=0
      t_sids=$(extract_record_sids "$tmp_file")
      set -f
      for s in $t_sids; do
        case " $exclude_sids " in
          *" $s "*) continue ;;
        esac
        if [ -z "$first_t" ]; then
          first_t="$s"
        else
          if [ "$rest_t_count" -lt 8 ]; then
            rest_t="${rest_t:+${rest_t} }${s}"
            rest_t_count=$((rest_t_count + 1))
          fi
        fi
      done
      set +f
      if [ -z "$first_t" ]; then
        rm -f "$tmp_file" 2>/dev/null || true
        return 0
      fi
      local t_pid t_tty t_pid_json="" t_tty_json=""
      t_pid=$(cat "$tmp_file" 2>/dev/null | LC_ALL=C grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' 2>/dev/null | head -n1 | grep -o '[0-9]*' || true)
      t_tty=$(cat "$tmp_file" 2>/dev/null | LC_ALL=C grep -o '"tty"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
      if printf '%s' "${t_pid:-}" | grep -Eq '^[0-9]{1,9}$'; then
        t_pid_json=",\"pid\":${t_pid}"
      fi
      if printf '%s' "${t_tty:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
        t_tty_json=",\"tty\":\"${t_tty}\""
      fi
      if [ -z "$rest_t" ]; then
        printf '{"vendor_session_id":"%s"%s%s}\n' "$first_t" "$t_pid_json" "$t_tty_json" > "$tmp_file" 2>/dev/null || true
      else
        printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s%s}\n' "$first_t" "$rest_t" "$t_pid_json" "$t_tty_json" > "$tmp_file" 2>/dev/null || true
      fi
    fi
    mv -f "$tmp_file" "$target" 2>/dev/null || rm -f "$tmp_file" 2>/dev/null || true
    return 0
  fi

  local primary_sid src_sids target_sids
  primary_sid=$(cat "$tmp_file" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
  if ! printf '%s' "$primary_sid" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
    primary_sid=""
  fi
  if [ -n "$primary_sid" ] && [ -n "$exclude_sids" ]; then
    case " $exclude_sids " in
      *" $primary_sid "*) primary_sid="" ;;
    esac
  fi

  src_sids=$(extract_record_sids "$tmp_file")
  target_sids=$(extract_record_sids "$target")

  local merged_sids=""
  set -f
  [ -n "$primary_sid" ] && merged_sids="$primary_sid"
  for s in $src_sids $target_sids; do
    if [ -n "$exclude_sids" ]; then
      case " $exclude_sids " in
        *" $s "*) continue ;;
      esac
    fi
    case " $merged_sids " in
      *" $s "*) ;;
      *) merged_sids="${merged_sids:+${merged_sids} }${s}" ;;
    esac
  done
  set +f

  local first_s="" rest_s="" rest_count=0
  for s in $merged_sids; do
    if [ -z "$first_s" ]; then
      first_s="$s"
    else
      if [ "$rest_count" -lt 8 ]; then
        rest_s="${rest_s:+${rest_s} }${s}"
        rest_count=$((rest_count + 1))
      fi
    fi
  done

  if [ -n "$first_s" ]; then
    local c_pid c_tty c_pid_json="" c_tty_json=""
    c_pid=$(cat "$tmp_file" "$target" 2>/dev/null | LC_ALL=C grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' 2>/dev/null | head -n1 | grep -o '[0-9]*' || true)
    c_tty=$(cat "$tmp_file" "$target" 2>/dev/null | LC_ALL=C grep -o '"tty"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    if printf '%s' "${c_pid:-}" | grep -Eq '^[0-9]{1,9}$'; then
      c_pid_json=",\"pid\":${c_pid}"
    fi
    if printf '%s' "${c_tty:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
      c_tty_json=",\"tty\":\"${c_tty}\""
    fi
    if [ -z "$rest_s" ]; then
      printf '{"vendor_session_id":"%s"%s%s}\n' "$first_s" "$c_pid_json" "$c_tty_json" > "$tmp_file" 2>/dev/null || true
    else
      printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s%s}\n' "$first_s" "$rest_s" "$c_pid_json" "$c_tty_json" > "$tmp_file" 2>/dev/null || true
    fi
    mv -f "$tmp_file" "$target" 2>/dev/null || rm -f "$tmp_file" 2>/dev/null || true
  else
    rm -f "$tmp_file" "$target" 2>/dev/null || true
  fi
}

# If Herdr actively owns this pane:
# - If an earlier fail-open wrote .vendor_active, attempt dismissal of the direct entry.
#   Only retire .vendor_active if dismissal was confirmed by HTTP 200, 404, or 410. If delivery fails
#   or Bartender is unavailable, keep .vendor_active intact so Herdr's reconciler stages
#   and retries dismissal under cache lock.
# - Otherwise suppress standalone reporting to prevent duplicate entries.
if is_herdr_owning_pane; then
  if [ -n "$VENDOR_ACTIVE" ] && [ -f "$VENDOR_ACTIVE" ]; then
    prev_sid=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    pend_sid=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"pending_dismissal_sid"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    prev_pid=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' 2>/dev/null | head -n1 | grep -o '[0-9]*' || true)
    prev_tty=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"tty"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)

    if ! printf '%s' "${prev_pid:-}" | grep -Eq '^[0-9]{1,9}$'; then
      prev_pid=""
    fi
    if ! printf '%s' "${prev_tty:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
      prev_tty=""
    fi
    [ -z "$AGENT_PID" ] && AGENT_PID="$prev_pid"
    [ -z "$AGENT_TTY" ] && AGENT_TTY="$prev_tty"

    valid_candidates=$(extract_record_sids "$VENDOR_ACTIVE")

    remaining_sids=""
    attempt_count=0
    for s in $valid_candidates; do
      if [ "$attempt_count" -lt 4 ] && has_time_remaining; then
        attempt_count=$((attempt_count + 1))
        if ! send_dismissal "$s"; then
          remaining_sids="${remaining_sids:+${remaining_sids} }${s}"
        fi
      else
        remaining_sids="${remaining_sids:+${remaining_sids} }${s}"
      fi
    done

    if [ -z "$remaining_sids" ]; then
      retire_vendor_file "$VENDOR_ACTIVE" "$valid_candidates"
    else
      # Rewrite .vendor_active with remaining SIDs so progress is preserved across events
      va_dir=$(dirname "$VENDOR_ACTIVE")
      mkdir -m 700 -p "$va_dir" 2>/dev/null || true
      TMP_VA=$(mktemp "${va_dir}/.va.tmp.XXXXXX" 2>/dev/null || true)
      if [ -n "$TMP_VA" ]; then
        chmod 0600 "$TMP_VA" 2>/dev/null || true
        first_rem=""
        rest_rem=""
        for s in $remaining_sids; do
          if [ -z "$first_rem" ]; then
            first_rem="$s"
          else
            rest_rem="${rest_rem:+${rest_rem} }${s}"
          fi
        done
        if [ -n "$first_rem" ]; then
          tty_json=""
          [ -n "${AGENT_TTY:-}" ] && tty_json=",\"tty\":\"${AGENT_TTY}\""
          pid_json=""
          [ -n "${AGENT_PID:-}" ] && pid_json=",\"pid\":${AGENT_PID}"
          if [ -z "$rest_rem" ]; then
            printf '{"vendor_session_id":"%s"%s%s}\n' "$first_rem" "$pid_json" "$tty_json" > "$TMP_VA" 2>/dev/null || true
          else
            printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s%s}\n' "$first_rem" "$rest_rem" "$pid_json" "$tty_json" > "$TMP_VA" 2>/dev/null || true
          fi
          confirmed_pane_sids=""
          set -f
          for s in $valid_candidates; do
            case " $remaining_sids " in
              *" $s "*) ;;
              *) confirmed_pane_sids="${confirmed_pane_sids:+${confirmed_pane_sids} }${s}" ;;
            esac
          done
          set +f
          merge_and_commit_vendor_file "$TMP_VA" "$VENDOR_ACTIVE" "$confirmed_pane_sids"
        fi
      fi
    fi
  fi

  case "$EVENT" in
    Stop|SessionEnd) printf '{"decision":""}\n' ;;
    *) printf '{}\n' ;;
  esac
  exit 0
fi

# In standalone mode (no HERDR_PANE_ID), track active session per process/TTY so Stop reuses the same SID
if [ -z "$CANONICAL_PANE" ]; then
  sa_ident="${AGENT_PID:-}:${AGENT_TTY:-}:${TERM_SESSION_ID:-}"
  if [ -z "$AGENT_PID" ] && [ -z "$AGENT_TTY" ]; then
    sa_ident="${PWD:-}:${TERM_SESSION_ID:-}"
  fi
  if [ "$sa_ident" != ":" ] && [ -n "$sa_ident" ]; then
    sa_hex=$(printf '%s' "$sa_ident" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n' | cut -c1-32)
    VENDOR_ACTIVE="${STATE_HOME}/standalone/${sa_hex}.active"
  fi
fi

# Sweep orphaned standalone .active files whose recording process has died or aged out
sweep_standalone_active() {
  # Never run sweep on Stop/SessionEnd (prioritize stopping session)
  case "$EVENT" in
    Stop|SessionEnd) return 0 ;;
  esac
  local sa_dir="${STATE_HOME}/standalone"
  [ -d "$sa_dir" ] || return 0
  local now
  now=$(date +%s 2>/dev/null || echo 0)
  local sweep_requests=0
  local max_sweep_requests=4

  for f in "$sa_dir"/*.active; do
    [ -f "$f" ] || continue
    [ -n "$VENDOR_ACTIVE" ] && [ "$f" = "$VENDOR_ACTIVE" ] && continue
    if [ "$sweep_requests" -ge "$max_sweep_requests" ]; then
      break
    fi

    local f_pid f_sid f_pend f_tty is_dead mtime age
    f_pid=$(cat "$f" 2>/dev/null | LC_ALL=C grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' 2>/dev/null | head -n1 | tr -cd '0-9')
    f_sid=$(cat "$f" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    f_pend=$(cat "$f" 2>/dev/null | LC_ALL=C grep -o '"pending_dismissal_sid"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    f_tty=$(cat "$f" 2>/dev/null | LC_ALL=C grep -o '"tty"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)

    # Determine age
    mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
    age=$(( now - mtime ))

    is_dead=0
    if [ -n "$f_pid" ]; then
      if ! kill -0 "$f_pid" 2>/dev/null; then
        is_dead=1
      elif [ "$age" -ge 43200 ]; then
        # 12h age horizon guards against PID reuse
        is_dead=1
      fi
    elif [ -n "$f_tty" ]; then
      # Record without PID but with TTY: verify whether terminal device is still open
      if [ -c "/dev/$f_tty" ]; then
        if [ "$age" -ge 86400 ] && ! ps -t "$f_tty" >/dev/null 2>&1; then
          is_dead=1
        fi
      else
        # Terminal device closed or destroyed: session is dead
        is_dead=1
      fi
    else
      # PID-less, TTY-less record: 12h (43200s) age horizon
      if [ "$age" -ge 43200 ]; then
        is_dead=1
      fi
    fi

    if [ "$is_dead" -eq 1 ]; then
      valid_sids=$(extract_record_sids "$f")

      # If dead record contains no valid SIDs at all (corrupted or unparseable), retire it immediately
      if [ -z "$valid_sids" ]; then
        retire_vendor_file "$f" ""
        continue
      fi

      local confirmed_sweep_sids=""
      for s in $valid_sids; do
        if [ "$sweep_requests" -ge "$max_sweep_requests" ] || ! has_time_remaining; then
          break
        fi
        sweep_requests=$((sweep_requests + 1))
        if send_dismissal "$s"; then
          confirmed_sweep_sids="${confirmed_sweep_sids:+${confirmed_sweep_sids} }${s}"
        fi
      done

      # Retire record with all successfully confirmed SIDs. If all SIDs were dismissed,
      # the file is removed; if only some were dismissed, the remaining unconfirmed SIDs are kept.
      if [ -n "$confirmed_sweep_sids" ]; then
        retire_vendor_file "$f" "$confirmed_sweep_sids"
      fi
    fi
  done
}
sweep_standalone_active

# Resolve Python interpreter safely:
# On macOS, /usr/bin/python3 is an xcrun stub that triggers GUI installation prompts
# if Command Line Tools are missing. We verify CLT before invoking /usr/bin/python3.
PYTHON_BIN="${AGY_HOOK_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    py_candidate=$(command -v python3)
    if [ "$py_candidate" = "/usr/bin/python3" ]; then
      if xcode-select -p >/dev/null 2>&1; then
        PYTHON_BIN="/usr/bin/python3"
      fi
    else
      PYTHON_BIN="$py_candidate"
    fi
  elif [ -x /usr/bin/python3 ] && xcode-select -p >/dev/null 2>&1; then
    PYTHON_BIN="/usr/bin/python3"
  fi
fi

PREV_VENDOR_SID=""
PENDING_DISMISSAL_SID=""
if [ -n "$VENDOR_ACTIVE" ] && [ -f "$VENDOR_ACTIVE" ]; then
  PREV_VENDOR_SID=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
  PENDING_DISMISSAL_SID=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"pending_dismissal_sid"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
  if [ "$PREV_VENDOR_SID" = "vendor_session_id" ] || ! printf '%s' "${PREV_VENDOR_SID:-}" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
    PREV_VENDOR_SID=""
  fi
  if [ -n "${PENDING_DISMISSAL_SID:-}" ]; then
    sanitized_pending=""
    set -f
    for s in $(printf '%s' "$PENDING_DISMISSAL_SID" | tr ',' ' '); do
      [ "$s" = "pending_dismissal_sid" ] && continue
      [ "$s" = "vendor_session_id" ] && continue
      if printf '%s' "$s" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
        sanitized_pending="${sanitized_pending:+${sanitized_pending} }${s}"
      fi
    done
    set +f
    PENDING_DISMISSAL_SID="$sanitized_pending"
  fi
fi

SID=""
payload=""

if [ -n "$PYTHON_BIN" ]; then
  py_output=$(printf '%s' "$RAW_INPUT" | EVENT="$EVENT" AGENT_PID="${AGENT_PID:-}" AGENT_TTY="${AGENT_TTY:-}" PREV_VENDOR_SID="${PREV_VENDOR_SID:-}" TERM_PROGRAM="${TERM_PROGRAM:-}" "$PYTHON_BIN" -c '
import hashlib, json, os, re, sys

CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)")
DCS_RE = re.compile(r"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c|$)")
ESC_RE = re.compile(r"\x1b[@-Z\\-~]?")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
INVISIBLE_RE = re.compile(
    "[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b\u200e\u200f\u2028-\u202e"
    "\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]")

def strip_controls(raw: object) -> str:
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    for p in (OSC_RE, DCS_RE, CSI_RE, ESC_RE, CONTROL_RE, INVISIBLE_RE):
        text = p.sub("", text)
    return text

def sanitize_str(raw: object, max_len: int = 120) -> str:
    return strip_controls(raw).strip()[:max_len]

raw_input = sys.stdin.read()
try:
    parse_ok = False
    try:
        d = json.loads(raw_input) if raw_input.strip() else {}
        if isinstance(d, dict):
            parse_ok = True
        else:
            d = {}
    except Exception:
        d = {}

    event = os.environ.get("EVENT") or ""
    if not event:
        sys.stdout.write("SKIP")
        sys.exit(0)

    # Antigravity lifecycle mapping:
    # PreInvocation -> turn starts, model thinking -> Working
    # PostInvocation -> tool calls finished, model turn complete -> Idle
    # Stop / SessionEnd -> execution terminated -> Ended
    if event == "PreInvocation":
        state = "Working"
        title = "Thinking..."
    elif event == "PostInvocation":
        state = "Idle"
        title = ""
    elif event in ("Stop", "SessionEnd"):
        state = "Ended"
        title = ""
    else:
        state = "Working" if "Pre" in event else "Idle"
        title = ""

    try:
        raw_pid = os.environ.get("AGENT_PID")
        pid = int(raw_pid) if raw_pid and raw_pid.isdigit() else None
    except Exception:
        pid = None

    # Working directory
    ws = d.get("workspacePaths")
    if isinstance(ws, list) and ws and isinstance(ws[0], str):
        raw_cwd = ws[0]
    else:
        raw_cwd = d.get("cwd") or os.getcwd()
    cwd = sanitize_str(raw_cwd, 256)

    # Conversation / Session ID
    conv_id = d.get("conversationId")
    if not (isinstance(conv_id, str) and re.match(r"^[a-zA-Z0-9_-]{16,64}\Z", conv_id)):
        pattern = chr(34) + "conversationId" + chr(34) + r"\s*:\s*" + chr(34) + r"([a-zA-Z0-9_-]{16,64})" + chr(34)
        m = re.search(pattern, raw_input)
        if m:
            conv_id = m.group(1)
        else:
            conv_id = None

    prev_sid = os.environ.get("PREV_VENDOR_SID") or ""
    if conv_id:
        session_id = conv_id
    elif event in ("Stop", "SessionEnd") and prev_sid and re.match(r"^[a-zA-Z0-9_-]{16,64}\Z", prev_sid):
        session_id = prev_sid
    elif parse_ok or event in ("Stop", "SessionEnd"):
        # Stable fallback session ID incorporating workspace, terminal session, TTY, and agent PID
        term_sess = os.environ.get("TERM_SESSION_ID", "")
        agent_tty = os.environ.get("AGENT_TTY", "")
        pid_str = str(pid) if pid else ""
        if term_sess or agent_tty or pid_str:
            seed = f"{term_sess}:{agent_tty}:{pid_str}"
        else:
            seed = f"{cwd}:{term_sess}:{agent_tty}:{pid_str}"
        h = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        session_id = f"agy-session-{h}"
    else:
        # Corrupted / unparseable payload on a non-Stop event without conversationId.
        # Output SKIP so the hook cleanly ignores it without inventing a phantom session.
        sys.stdout.write("SKIP")
        sys.exit(0)

    # Terminal mapping
    term_map = {
        "iTerm.app": "iTerm",
        "Apple_Terminal": "Terminal",
        "vscode": "VS Code",
        "WarpTerminal": "Warp",
        "ghostty": "Ghostty",
        "Hyper": "Hyper",
        "WezTerm": "WezTerm",
        "kitty": "kitty",
        "tabby": "Tabby",
        "alacritty": "Alacritty",
    }
    raw_term = os.environ.get("TERM_PROGRAM") or ""
    sanitized_term = sanitize_str(raw_term, 64)
    clean_term = sanitize_str(term_map.get(sanitized_term, sanitized_term), 64)

    payload_json = json.dumps({
        "state": state,
        "agent": "Antigravity",
        "event": event,
        "session_id": session_id,
        "cwd": cwd,
        "title": title,
        "terminal": clean_term,
        "pid": pid,
    })

    sys.stdout.write(f"{session_id}\n{payload_json}")
except Exception:
    sys.stdout.write("SKIP")
    sys.exit(0)
' 2>/dev/null || true)
  if [ "$py_output" = "SKIP" ]; then
    case "$EVENT" in
      Stop|SessionEnd) printf '{"decision":""}\n' ;;
      *) printf '{}\n' ;;
    esac
    exit 0
  fi
  if [ -n "$py_output" ]; then
    SID=$(printf '%s\n' "$py_output" | head -n1)
    payload=$(printf '%s\n' "$py_output" | tail -n +2)
  fi
fi

# Resilient fallback if Python is unavailable
if [ -z "${payload:-}" ]; then
  if [ -z "$EVENT" ]; then
    case "$EVENT" in
      Stop|SessionEnd) printf '{"decision":""}\n' ;;
      *) printf '{}\n' ;;
    esac
    exit 0
  fi
  case "$EVENT" in
    Stop|SessionEnd) FB_STATE="Ended" ;;
    PostInvocation) FB_STATE="Idle" ;;
    *) FB_STATE="Working" ;;
  esac
  # Reuse PREV_VENDOR_SID for Stop/SessionEnd if available
  if { [ "$EVENT" = "Stop" ] || [ "$EVENT" = "SessionEnd" ]; } && [ -n "$PREV_VENDOR_SID" ]; then
    FALLBACK_SID="$PREV_VENDOR_SID"
  else
    seed_str="${PWD:-}:${AGENT_PID:-}:${AGENT_TTY:-}:${TERM_SESSION_ID:-}"
    h=""
    if command -v shasum >/dev/null 2>&1; then
      h=$(printf '%s' "$seed_str" | shasum -a 256 2>/dev/null | cut -c1-24)
    elif command -v sha256sum >/dev/null 2>&1; then
      h=$(printf '%s' "$seed_str" | sha256sum 2>/dev/null | cut -c1-24)
    elif command -v cksum >/dev/null 2>&1; then
      h=$(printf '%s' "$seed_str" | cksum 2>/dev/null | tr -cd '0-9')
    fi
    if ! printf '%s' "$h" | LC_ALL=C grep -Eq '^[0-9a-zA-Z]{16,64}$'; then
      h="fb$(printf '%s' "$seed_str" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n')0123456789abcdef"
      h=$(printf '%s' "$h" | cut -c1-24)
    fi
    FALLBACK_SID="agy-fallback-${h}"
  fi
  SID="$FALLBACK_SID"
  payload="{\"state\":\"${FB_STATE}\",\"agent\":\"Antigravity\",\"event\":\"${EVENT}\",\"session_id\":\"${FALLBACK_SID}\"}"
fi

# On non-Stop events, record or update .vendor_active
if [ -n "$VENDOR_ACTIVE" ] && [ -n "$SID" ] && [ "$EVENT" != "Stop" ] && [ "$EVENT" != "SessionEnd" ]; then
  # Only record if SID matches standard vendor UUID regex (16-64 chars)
  if printf '%s' "$SID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
    set -f
    to_dismiss=""
    local_count=0
    for s in $PREV_VENDOR_SID $PENDING_DISMISSAL_SID; do
      [ -n "$s" ] || continue
      [ "$s" = "$SID" ] && continue
      if printf '%s' "$s" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
        case " $to_dismiss " in
          *" $s "*) ;;
          *)
            if [ "$local_count" -lt 8 ]; then
              to_dismiss="${to_dismiss:+${to_dismiss} }${s}"
              local_count=$((local_count + 1))
            else
              printf '[herdr-bartender] Warning: pending dismissal cap (8) reached; dropping oldest SID %s\n' "$s" >&2
            fi
            ;;
        esac
      fi
    done
    set +f

    still_pending=""
    # Attempt dismissal for up to 4 SIDs per event, preserving any remaining (un-attempted or failed) ones
    attempt_count=0
    for s in $to_dismiss; do
      if [ "$attempt_count" -lt 4 ] && has_time_remaining; then
        attempt_count=$((attempt_count + 1))
        if ! send_dismissal "$s"; then
          still_pending="${still_pending:+${still_pending} }${s}"
        fi
      else
        still_pending="${still_pending:+${still_pending} }${s}"
      fi
    done

    va_dir=$(dirname "$VENDOR_ACTIVE")
    mkdir -m 700 -p "$va_dir" 2>/dev/null || true
    TMP_VA=$(mktemp "${va_dir}/.va.tmp.XXXXXX" 2>/dev/null || true)
    if [ -n "$TMP_VA" ]; then
      chmod 0600 "$TMP_VA" 2>/dev/null || true
      tty_json=""
      [ -n "${AGENT_TTY:-}" ] && tty_json=",\"tty\":\"${AGENT_TTY}\""
      if [ -z "$still_pending" ]; then
        if [ -n "$AGENT_PID" ]; then
          printf '{"vendor_session_id":"%s","pid":%s%s}\n' "$SID" "$AGENT_PID" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        else
          printf '{"vendor_session_id":"%s"%s}\n' "$SID" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        fi
      else
        # Dismissal of previous session(s) could not be completed; record both so subsequent events/Stop retry dismissing them,
        # while still recording and advancing to current session.
        if [ -n "$AGENT_PID" ]; then
          printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s","pid":%s%s}\n' "$SID" "$still_pending" "$AGENT_PID" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        else
          printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s}\n' "$SID" "$still_pending" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        fi
      fi
      confirmed_nonstop_sids=""
      set -f
      for s in $to_dismiss; do
        case " $still_pending " in
          *" $s "*) ;;
          *) confirmed_nonstop_sids="${confirmed_nonstop_sids:+${confirmed_nonstop_sids} }${s}" ;;
        esac
      done
      set +f
      merge_and_commit_vendor_file "$TMP_VA" "$VENDOR_ACTIVE" "$confirmed_nonstop_sids"
    fi
  fi
fi

# Send event synchronously with tight timeout. Strictly loopback, no proxies, no redirects.
http_code="000"
if [ "$BARTENDER_UNREACHABLE" != "1" ] && has_time_remaining && is_bartender_alive; then
  TOTAL_NETWORK_REQUESTS=$((TOTAL_NETWORK_REQUESTS + 1))
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    --noproxy '*' \
    --max-redirs 0 \
    --proto =http \
    --connect-timeout 0.15 \
    --max-time 0.5 \
    -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "$payload" 2>/dev/null || true)
  case "$http_code" in
    200|404|410) ;;
    000*|"")
      http_code="000"
      BARTENDER_UNREACHABLE=1
      ;;
  esac
fi

# On Stop/SessionEnd, retire .vendor_active ONLY if dismissal was confirmed by HTTP 200, 404, or 410.
# If delivery timed out or failed, keep .vendor_active intact so Herdr reconciler retries.
# If a pending or previous session differs from SID, both must be dismissed before retiring.
if [ -n "$VENDOR_ACTIVE" ] && { [ "$EVENT" = "Stop" ] || [ "$EVENT" = "SessionEnd" ]; }; then
  set -f
  to_dismiss=""
  local_count=0
  for s in $PREV_VENDOR_SID $PENDING_DISMISSAL_SID; do
    [ -n "$s" ] || continue
    [ "$s" = "$SID" ] && continue
    if printf '%s' "$s" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
      case " $to_dismiss " in
        *" $s "*) ;;
        *)
          if [ "$local_count" -lt 8 ]; then
            to_dismiss="${to_dismiss:+${to_dismiss} }${s}"
            local_count=$((local_count + 1))
          else
            printf '[herdr-bartender] Warning: pending dismissal cap (8) reached; dropping oldest SID %s\n' "$s" >&2
          fi
          ;;
      esac
    fi
  done
  set +f

  failed_priors=""
  succeeded_priors=""
  attempt_count=0
  for s in $to_dismiss; do
    if [ "$attempt_count" -lt 4 ] && has_time_remaining; then
      attempt_count=$((attempt_count + 1))
      if send_dismissal "$s"; then
        succeeded_priors="${succeeded_priors:+${succeeded_priors} }${s}"
      else
        failed_priors="${failed_priors:+${failed_priors} }${s}"
      fi
    else
      failed_priors="${failed_priors:+${failed_priors} }${s}"
    fi
  done

  current_ok=0
  case "$http_code" in
    200|404|410) current_ok=1 ;;
  esac

  confirmed_sids=""
  [ "$current_ok" -eq 1 ] && confirmed_sids="$SID"
  for s in $succeeded_priors; do
    confirmed_sids="${confirmed_sids:+${confirmed_sids} }${s}"
  done
  if [ -n "$PREV_VENDOR_SID" ]; then
    case " $confirmed_sids " in
      *" $PREV_VENDOR_SID "*) ;;
      *)
        if [ "$current_ok" -eq 1 ] && [ "$PREV_VENDOR_SID" = "$SID" ]; then
          confirmed_sids="${confirmed_sids:+${confirmed_sids} }${PREV_VENDOR_SID}"
        fi
        ;;
    esac
  fi

  if [ "$current_ok" -eq 1 ] && [ -z "$failed_priors" ]; then
    retire_vendor_file "$VENDOR_ACTIVE" "$confirmed_sids"
  else
    # Some dismissals could not be confirmed: rewrite .vendor_active with remaining un-dismissed SIDs
    va_dir=$(dirname "$VENDOR_ACTIVE")
    mkdir -m 700 -p "$va_dir" 2>/dev/null || true
    TMP_VA=$(mktemp "${va_dir}/.va.tmp.XXXXXX" 2>/dev/null || true)
    if [ -n "$TMP_VA" ]; then
      chmod 0600 "$TMP_VA" 2>/dev/null || true
      all_failed=""
      [ "$current_ok" -ne 1 ] && all_failed="$SID"
      for s in $failed_priors; do
        all_failed="${all_failed:+${all_failed} }${s}"
      done
      first_f=""
      rest_f=""
      for s in $all_failed; do
        if [ -z "$first_f" ]; then
          first_f="$s"
        else
          rest_f="${rest_f:+${rest_f} }${s}"
        fi
      done
      if [ -n "$first_f" ]; then
        tty_json=""
        if printf '%s' "${AGENT_TTY:-}" | grep -Eq '^[a-zA-Z0-9/_.-]{1,32}$'; then
          tty_json=",\"tty\":\"${AGENT_TTY}\""
        fi
        pid_json=""
        if printf '%s' "${AGENT_PID:-}" | grep -Eq '^[0-9]{1,9}$'; then
          pid_json=",\"pid\":${AGENT_PID}"
        fi
        if [ -z "$rest_f" ]; then
          printf '{"vendor_session_id":"%s"%s%s}\n' "$first_f" "$pid_json" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        else
          printf '{"vendor_session_id":"%s","pending_dismissal_sid":"%s"%s%s}\n' "$first_f" "$rest_f" "$pid_json" "$tty_json" > "$TMP_VA" 2>/dev/null || true
        fi
        merge_and_commit_vendor_file "$TMP_VA" "$VENDOR_ACTIVE" "$confirmed_sids"
      else
        rm -f "$TMP_VA" 2>/dev/null || true
      fi
    fi
  fi
fi

# Emit expected JSON response to stdout for Antigravity lifecycle
case "$EVENT" in
  Stop|SessionEnd) printf '{"decision":""}\n' ;;
  *) printf '{}\n' ;;
esac

exit 0
