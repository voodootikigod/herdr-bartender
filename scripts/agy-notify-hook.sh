#!/bin/bash
set -u

# Drain bounded stdin safely (cap at 64 KiB) once at the start of execution
HOOK_JSON=$(head -c 65536 2>/dev/null || true)
if [ -z "${HOOK_JSON:-}" ]; then
  HOOK_JSON='{}'
fi

# Resolve EVENT from AGY_HOOK_EVENT, argv $1, or top-level hook_event_name in JSON
EVENT="${AGY_HOOK_EVENT:-${1:-}}"
if [ -z "$EVENT" ]; then
  EVENT=$(printf '%s' "$HOOK_JSON" | LC_ALL=C grep -o '"hook_event_name"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
fi

# Whitelist EVENT strictly to prevent injection into JSON or logic
case "$EVENT" in
  PreInvocation|PreToolUse|PostInvocation|Stop) ;;
  *) EVENT="" ;;
esac

# Strictly loopback only (Plan §8: literal loopback, no hostname resolution, no remote proxy)
HOST="127.0.0.1"
PORT="${NOTCHBAR_AGENTS_PORT:-7823}"
if ! printf '%s' "$PORT" | grep -Eq '^[0-9]{1,5}$' || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  PORT=7823
fi

# Check if Bartender 6 / Bartender is running (matching Python is_bartender_alive)
is_bartender_alive() {
  pgrep -xi "Bartender 6" >/dev/null 2>&1 \
    || pgrep -xi "Bartender" >/dev/null 2>&1 \
    || pgrep -f '^[^[:space:]]*/Bartender( 6)?\.app/Contents/MacOS/' >/dev/null 2>&1
}

# Resolve canonical pane ID and paths if HERDR_PANE_ID is set
CANONICAL_PANE=""
HEX_PANE=""
STATE_HOME="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
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

# If Herdr actively owns this pane:
# - If an earlier fail-open wrote .vendor_active, immediately dismiss the direct entry now that Herdr has taken over!
# - Otherwise suppress standalone reporting to prevent duplicate entries
if is_herdr_owning_pane; then
  if [ -n "$VENDOR_ACTIVE" ] && [ -f "$VENDOR_ACTIVE" ]; then
    prev_sid=$(printf '%s' "$(cat "$VENDOR_ACTIVE" 2>/dev/null || true)" | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    rm -f "$VENDOR_ACTIVE" 2>/dev/null || true
    if [ -n "$prev_sid" ] && is_bartender_alive; then
      curl -s \
        --noproxy '*' \
        --max-redirs 0 \
        --proto =http \
        --connect-timeout 0.15 \
        --max-time 0.5 \
        -X POST "http://${HOST}:${PORT}/event" \
        -H 'Content-Type: application/json' \
        --data-raw "{\"state\":\"Ended\",\"agent\":\"Antigravity\",\"session_id\":\"${prev_sid}\"}" >/dev/null 2>&1 || true
    fi
  fi

  case "$EVENT" in
    PreToolUse) printf '{"decision":"allow"}\n' ;;
    Stop) printf '{"decision":""}\n' ;;
    *) printf '{}\n' ;;
  esac
  exit 0
fi

# Find agent PID: match ONLY the binary basename (agy, antigravity, Antigravity),
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

AGENT_PID="$(find_agent_pid 2>/dev/null || true)"
if ! printf '%s' "${AGENT_PID:-}" | grep -Eq '^[0-9]+$'; then
  AGENT_PID=""
fi

# Build Bartender Top Shelf JSON payload with comprehensive terminal/bidi control stripping
# and strict length caps.
payload=$(
  EVENT="$EVENT" HOOK_JSON="$HOOK_JSON" TERM_PROGRAM="${TERM_PROGRAM:-}" AGENT_PID="${AGENT_PID:-}" /usr/bin/python3 - <<'PY' 2>/dev/null
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

try:
    d = json.loads(os.environ.get("HOOK_JSON") or "{}")
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}

event = os.environ.get("EVENT") or d.get("hook_event_name") or ""
event = sanitize_str(event, 64)

# Antigravity lifecycle mapping:
# PreInvocation -> turn starts, model thinking -> Working
# PreToolUse -> tool call started -> Working
# PostInvocation -> tool calls finished, model turn complete -> Idle
# Stop -> execution loop terminated -> Ended
if event == "PreInvocation":
    state = "Working"
    title = "Thinking..."
elif event == "PreToolUse":
    state = "Working"
    tool_call = d.get("toolCall")
    tool_name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    clean_tool = sanitize_str(tool_name, 64)
    title = f"Tool: {clean_tool}" if clean_tool else "Working"
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

# Conversation / Session ID:
# Strict format validation: 16 to 64 chars [a-zA-Z0-9_-] (matching VENDOR_UUID_REGEX)
conv_id = d.get("conversationId")
if isinstance(conv_id, str) and re.match(r'^[a-zA-Z0-9_-]{16,64}\Z', conv_id):
    session_id = conv_id
else:
    # Deterministic fallback per workspace and terminal session (independent of transient subshell PIDs)
    seed = f"{cwd}:{os.environ.get('TERM_SESSION_ID', '')}:{os.environ.get('TTY', '')}"
    h = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]
    session_id = f"agy-session-{h}"

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
clean_term = sanitize_str(term_map.get(raw_term, raw_term), 64)

sys.stdout.write(json.dumps({
    "state": state,
    "agent": "Antigravity",
    "event": event,
    "session_id": session_id,
    "cwd": cwd,
    "title": title,
    "terminal": clean_term,
    "pid": pid,
}))
PY
)

# Resilient fallback if Python is unavailable
if [ -z "${payload:-}" ]; then
  case "$EVENT" in
    Stop) FB_STATE="Ended" ;;
    PostInvocation) FB_STATE="Idle" ;;
    *) FB_STATE="Working" ;;
  esac
  FALLBACK_SID="agy-session-fallback-default"
  payload="{\"state\":\"${FB_STATE}\",\"agent\":\"Antigravity\",\"event\":\"${EVENT}\",\"session_id\":\"${FALLBACK_SID}\"}"
fi

# Extract session_id from payload for .vendor_active management
SID=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import json, sys; d=json.load(sys.stdin); sys.stdout.write(d.get("session_id") or "")' 2>/dev/null || echo "agy-session-fallback-default")

# Record or retire .vendor_active if running within a Herdr pane during a fail-open window
if [ -n "$VENDOR_ACTIVE" ] && [ -n "$SID" ]; then
  if [ "$EVENT" = "Stop" ]; then
    rm -f "$VENDOR_ACTIVE" 2>/dev/null || true
  else
    mkdir -m 700 -p "${STATE_HOME}/panes" 2>/dev/null || true
    TMP_VA=$(mktemp "${STATE_HOME}/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
    if [ -n "$TMP_VA" ]; then
      chmod 0600 "$TMP_VA" 2>/dev/null || true
      printf '{"vendor_session_id":"%s"}\n' "$SID" > "$TMP_VA" 2>/dev/null || true
      mv -f "$TMP_VA" "$VENDOR_ACTIVE" 2>/dev/null || rm -f "$TMP_VA" 2>/dev/null || true
    fi
  fi
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
