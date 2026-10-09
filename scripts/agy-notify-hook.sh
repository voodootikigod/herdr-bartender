#!/bin/bash
set -u

# Strictly loopback only (Plan §8: literal loopback, no hostname resolution, no remote proxy)
HOST="127.0.0.1"
PORT="${NOTCHBAR_AGENTS_PORT:-7823}"
if ! printf '%s' "$PORT" | grep -Eq '^[0-9]{1,5}$' || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  PORT=7823
fi

EVENT="${AGY_HOOK_EVENT:-${1:-}}"

# Check if Bartender 6 / Bartender is running (matching Python is_bartender_alive)
is_bartender_alive() {
  pgrep -xi "Bartender 6" >/dev/null 2>&1 \
    || pgrep -xi "Bartender" >/dev/null 2>&1 \
    || pgrep -f '^[^[:space:]]*/Bartender( 6)?\.app/Contents/MacOS/' >/dev/null 2>&1
}

# Check if Herdr is demonstrably healthy and actively owns this pane.
# If Herdr is dead, disabled, or the marker is stale/missing, fail open so standalone
# agy still reports to Bartender Top Shelf.
is_herdr_owning_pane() {
  [ -n "${HERDR_PANE_ID:-}" ] || return 1
  printf '%s' "$HERDR_PANE_ID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$' || return 1

  local canonical_pane=""
  if printf '%s' "$HERDR_PANE_ID" | grep -q ':'; then
    canonical_pane="$HERDR_PANE_ID"
  elif [ -n "${HERDR_WORKSPACE_ID:-}" ]; then
    canonical_pane="${HERDR_WORKSPACE_ID}:${HERDR_PANE_ID}"
  fi
  [ -n "$canonical_pane" ] || return 1
  printf '%s' "$canonical_pane" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$' || return 1

  local state_home="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
  local hex_pane
  hex_pane=$(printf '%s' "$canonical_pane" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n')
  local pane_marker="${state_home}/panes/${hex_pane}"

  # Flag files: if disabled or delivery down, do not suppress
  [ -e "${state_home}/DISABLED" ] && return 1
  [ -e "${state_home}/DELIVERY_DOWN" ] && return 1

  # Marker must exist and not be failed
  [ -f "$pane_marker" ] && [ ! -L "$pane_marker" ] || return 1
  [ -e "${pane_marker}.failed" ] && return 1

  # Marker freshness: mtime within 60s
  local mtime now age
  mtime=$(stat -c %Y "$pane_marker" 2>/dev/null || stat -f %m "$pane_marker" 2>/dev/null || echo 0)
  now=$(date +%s 2>/dev/null || echo 0)
  age=$(( now - mtime ))
  [ "$age" -ge 0 ] && [ "$age" -lt 60 ] || return 1

  # Herdr process must be alive
  if pgrep -a -xi "herdr" >/dev/null 2>&1 || pgrep -a -f '^[^[:space:]]*/Herdr\.app/Contents/MacOS/' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# If Herdr actively owns this pane, suppress standalone reporting to prevent duplicate entries
if is_herdr_owning_pane; then
  # Drain bounded stdin to avoid EPIPE in caller
  { head -c 65536 >/dev/null 2>&1 || cat >/dev/null 2>&1; } || true
  case "$EVENT" in
    PreToolUse) printf '{"decision":"allow"}\n' ;;
    Stop) printf '{"decision":""}\n' ;;
    *) printf '{}\n' ;;
  esac
  exit 0
fi

# Drain bounded stdin safely (cap at 64 KiB)
HOOK_JSON=$(head -c 65536 2>/dev/null || true)
if [ -z "${HOOK_JSON:-}" ]; then
  HOOK_JSON='{}'
fi

# Find agent PID (search for agy or fallback to PPID)
find_agent_pid() {
  local pid=$PPID
  local max=6
  while [ "$max" -gt 0 ] && [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    local name
    name=$(ps -o comm= -p "$pid" 2>/dev/null)
    case "$name" in
      *agy*|*antigravity*|*Antigravity*) echo "$pid"; return 0 ;;
    esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    max=$((max - 1))
  done
  echo "${PPID:-$$}"
  return 0
}

AGENT_PID="$(find_agent_pid 2>/dev/null || echo "$$")"

# Build Bartender Top Shelf JSON payload
payload=$(
  EVENT="$EVENT" HOOK_JSON="$HOOK_JSON" TERM_PROGRAM="${TERM_PROGRAM:-}" AGENT_PID="${AGENT_PID:-}" /usr/bin/python3 - <<'PY' 2>/dev/null
import json, os, sys

try:
    d = json.loads(os.environ.get("HOOK_JSON") or "{}")
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}

event = os.environ.get("EVENT") or d.get("hook_event_name") or ""

# Map state and title
if event == "PreInvocation":
    state = "Working"
    title = "Thinking..."
elif event == "PreToolUse":
    state = "Working"
    tool_call = d.get("toolCall") or {}
    tool_name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    title = f"Tool: {tool_name}" if tool_name else "Working"
elif event == "PostInvocation":
    state = "Idle"
    title = ""
elif event == "Stop":
    state = "Ended"
    title = ""
else:
    state = "Working" if "Pre" in event else "Idle"
    title = ""

try:
    pid = int(os.environ.get("AGENT_PID") or 0) or None
except Exception:
    pid = None

# Conversation / Session ID (per-process fallback prevents cross-session collision)
conv_id = d.get("conversationId") or ""
session_id = f"agy:{conv_id}" if conv_id else f"agy:pid:{pid or os.getpid()}"

# Working directory
ws = d.get("workspacePaths")
if isinstance(ws, list) and ws and isinstance(ws[0], str):
    cwd = ws[0]
else:
    cwd = d.get("cwd") or os.getcwd()

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

sys.stdout.write(json.dumps({
    "state": state,
    "agent": "Antigravity",
    "event": event,
    "session_id": session_id,
    "cwd": cwd,
    "title": title,
    "terminal": term_map.get(raw_term, raw_term),
    "pid": pid,
}))
PY
)

if [ -z "${payload:-}" ]; then
  payload="{\"state\":\"Working\",\"agent\":\"Antigravity\",\"session_id\":\"agy:pid:${AGENT_PID}\"}"
fi

# Send event synchronously with tight timeout. Strictly loopback, no proxies, no redirects.
# Synchronous delivery guarantees events arrive in strict chronological order and
# cannot race or land after Stop.
if is_bartender_alive; then
  curl -s \
    --noproxy '*' \
    --max-redirs 0 \
    --proto =http \
    --connect-timeout 0.15 \
    --max-time 0.5 \
    -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "$payload" >/dev/null 2>&1 || true
fi

# Emit expected JSON response to stdout for Antigravity lifecycle
case "$EVENT" in
  PreToolUse) printf '{"decision":"allow"}\n' ;;
  Stop) printf '{"decision":""}\n' ;;
  *) printf '{}\n' ;;
esac

exit 0
