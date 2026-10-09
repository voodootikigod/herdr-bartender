#!/bin/bash
set -u

HOST="${NOTCHBAR_AGENTS_HOST:-127.0.0.1}"
PORT="${NOTCHBAR_AGENTS_PORT:-7823}"
EVENT="${AGY_HOOK_EVENT:-${1:-}}"

# If running inside a Herdr pane, suppress direct Bartender notifications.
# Herdr's native agent detection and herdr-bartender handle Top Shelf deduplication.
if [ -n "${HERDR_PANE_ID:-}" ]; then
  case "$EVENT" in
    PreToolUse) printf '{"decision":"allow"}\n' ;;
    Stop) printf '{"decision":""}\n' ;;
    *) printf '{}\n' ;;
  esac
  exit 0
fi

# Drain stdin safely
HOOK_JSON=$(cat 2>/dev/null || true)
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

# Conversation / Session ID
conv_id = d.get("conversationId") or ""
session_id = f"agy:{conv_id}" if conv_id else "agy:default"

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

try:
    pid = int(os.environ.get("AGENT_PID") or 0) or None
except Exception:
    pid = None

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
  payload="{\"state\":\"Working\",\"agent\":\"Antigravity\"}"
fi

# Send event to Bartender NotchBar / Top Shelf server
if [ "$EVENT" = "Stop" ]; then
  curl -s -m 2 -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "$payload" >/dev/null 2>&1
else
  curl -s -m 1 -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "$payload" >/dev/null 2>&1 &
fi

# Emit expected JSON response to stdout for Antigravity lifecycle
case "$EVENT" in
  PreToolUse) printf '{"decision":"allow"}\n' ;;
  Stop) printf '{"decision":""}\n' ;;
  *) printf '{}\n' ;;
esac

exit 0
