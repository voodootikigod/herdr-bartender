#!/bin/bash
set -u

# Drain stdin completely to prevent EPIPE to writing agent processes
RAW_INPUT="$(cat 2>/dev/null || true)"

# Resolve EVENT from AGY_HOOK_EVENT, argv $1, or top-level hook_event_name in JSON
EVENT="${AGY_HOOK_EVENT:-${1:-}}"
if [ -z "$EVENT" ]; then
  EVENT=$(printf '%s' "$RAW_INPUT" | LC_ALL=C grep -o '"hook_event_name"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
fi

# Whitelist EVENT strictly to prevent injection into JSON or logic
case "$EVENT" in
  PreInvocation|PreToolUse|PostInvocation|Stop|SessionEnd) ;;
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

# Safely claim and retire a .vendor_active file (mirroring vendor.retire_vendor_file).
# Checks that the claimed file still holds expected_sid; if rewritten concurrently,
# restores the newer record so it is never lost.
retire_vendor_file() {
  local target="$1"
  local expected_sid="$2"
  [ -f "$target" ] || return 0
  local claim="${target}.claim-$$-$(date +%s%N 2>/dev/null || date +%s 2>/dev/null || echo $$)"
  if mv -f "$target" "$claim" 2>/dev/null; then
    if grep -q "\"vendor_session_id\"[[:space:]]*:[[:space:]]*\"${expected_sid}\"" "$claim" 2>/dev/null; then
      rm -f "$claim" 2>/dev/null || true
    else
      # Newer record written concurrently: restore it so newer session is never lost
      mv -n "$claim" "$target" 2>/dev/null || rm -f "$claim" 2>/dev/null || true
    fi
  fi
}

# If Herdr actively owns this pane:
# - If an earlier fail-open wrote .vendor_active, attempt dismissal of the direct entry.
#   Only retire .vendor_active if dismissal was confirmed by HTTP 200. If delivery fails
#   or Bartender is unavailable, keep .vendor_active intact so Herdr's reconciler stages
#   and retries dismissal under cache lock.
# - Otherwise suppress standalone reporting to prevent duplicate entries.
if is_herdr_owning_pane; then
  if [ -n "$VENDOR_ACTIVE" ] && [ -f "$VENDOR_ACTIVE" ]; then
    prev_sid=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
    if [ -n "$prev_sid" ] && is_bartender_alive; then
      http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        --noproxy '*' \
        --max-redirs 0 \
        --proto =http \
        --connect-timeout 0.15 \
        --max-time 0.5 \
        -X POST "http://${HOST}:${PORT}/event" \
        -H 'Content-Type: application/json' \
        --data-raw "{\"state\":\"Ended\",\"agent\":\"Antigravity\",\"session_id\":\"${prev_sid}\"}" 2>/dev/null || echo "000")
      if [ "$http_code" = "200" ]; then
        retire_vendor_file "$VENDOR_ACTIVE" "$prev_sid"
      fi
    fi
  fi

  case "$EVENT" in
    PreToolUse) printf '{"decision":"allow"}\n' ;;
    Stop|SessionEnd) printf '{"decision":""}\n' ;;
    *) printf '{}\n' ;;
  esac
  exit 0
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

AGENT_PID="${AGENT_PID:-$(find_agent_pid 2>/dev/null || true)}"
if ! printf '%s' "${AGENT_PID:-}" | grep -Eq '^[0-9]+$'; then
  AGENT_PID=""
fi

# Resolve controlling terminal for multi-session collision avoidance
AGENT_TTY="${AGENT_TTY:-}"
if [ -z "$AGENT_TTY" ]; then
  if [ -n "$AGENT_PID" ]; then
    AGENT_TTY=$(ps -o tty= -p "$AGENT_PID" 2>/dev/null | tr -d ' \t\n' || true)
  fi
  if [ -z "$AGENT_TTY" ] || [ "$AGENT_TTY" = "?" ] || [ "$AGENT_TTY" = "??" ]; then
    AGENT_TTY=$(ps -o tty= -p $$ 2>/dev/null | tr -d ' \t\n' || true)
  fi
fi

# In standalone mode (no HERDR_PANE_ID), track active session per process/TTY so Stop reuses the same SID
if [ -z "$CANONICAL_PANE" ]; then
  sa_ident="${AGENT_PID:-}:${AGENT_TTY:-}:${TERM_SESSION_ID:-}"
  if [ "$sa_ident" != "::" ]; then
    sa_hex=$(printf '%s' "$sa_ident" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n' | cut -c1-32)
    VENDOR_ACTIVE="${STATE_HOME}/panes/sa_${sa_hex}.vendor_active"
  fi
fi

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
if [ -n "$VENDOR_ACTIVE" ] && [ -f "$VENDOR_ACTIVE" ]; then
  PREV_VENDOR_SID=$(cat "$VENDOR_ACTIVE" 2>/dev/null | LC_ALL=C grep -o '"vendor_session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -n1 | cut -d'"' -f4 || true)
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
    # PreToolUse -> tool call started -> Working
    # PostInvocation -> tool calls finished, model turn complete -> Idle
    # Stop / SessionEnd -> execution terminated -> Ended
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
    if event in ("Stop", "SessionEnd") and prev_sid and re.match(r"^[a-zA-Z0-9_-]{16,64}\Z", prev_sid):
        session_id = prev_sid
    elif conv_id:
        session_id = conv_id
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
    clean_term = sanitize_str(term_map.get(raw_term, raw_term), 64)

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
      PreToolUse) printf '{"decision":"allow"}\n' ;;
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
      PreToolUse) printf '{"decision":"allow"}\n' ;;
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

# On non-Stop events, record or update .vendor_active during fail-open window
if [ -n "$VENDOR_ACTIVE" ] && [ -n "$SID" ] && [ "$EVENT" != "Stop" ] && [ "$EVENT" != "SessionEnd" ]; then
  # Only record if SID matches standard vendor UUID regex (16-64 chars)
  if printf '%s' "$SID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
    can_write_va=1
    # If an existing record holds an older, different session ID, dismiss the older session first
    if [ -n "$PREV_VENDOR_SID" ] && [ "$PREV_VENDOR_SID" != "$SID" ]; then
      if is_bartender_alive; then
        old_code=$(curl -s -o /dev/null -w "%{http_code}" \
          --noproxy '*' \
          --max-redirs 0 \
          --proto =http \
          --connect-timeout 0.15 \
          --max-time 0.5 \
          -X POST "http://${HOST}:${PORT}/event" \
          -H 'Content-Type: application/json' \
          --data-raw "{\"state\":\"Ended\",\"agent\":\"Antigravity\",\"session_id\":\"${PREV_VENDOR_SID}\"}" 2>/dev/null || echo "000")
        if [ "$old_code" != "200" ]; then
          can_write_va=0
        fi
      else
        can_write_va=0
      fi
    fi

    if [ "$can_write_va" -eq 1 ]; then
      mkdir -m 700 -p "${STATE_HOME}/panes" 2>/dev/null || true
      TMP_VA=$(mktemp "${STATE_HOME}/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
      if [ -n "$TMP_VA" ]; then
        chmod 0600 "$TMP_VA" 2>/dev/null || true
        printf '{"vendor_session_id":"%s"}\n' "$SID" > "$TMP_VA" 2>/dev/null || true
        mv -f "$TMP_VA" "$VENDOR_ACTIVE" 2>/dev/null || rm -f "$TMP_VA" 2>/dev/null || true
      fi
    fi
  fi
fi

# Send event synchronously with tight timeout. Strictly loopback, no proxies, no redirects.
http_code="000"
if is_bartender_alive; then
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    --noproxy '*' \
    --max-redirs 0 \
    --proto =http \
    --connect-timeout 0.15 \
    --max-time 0.5 \
    -X POST "http://${HOST}:${PORT}/event" \
    -H 'Content-Type: application/json' \
    --data-raw "$payload" 2>/dev/null || echo "000")
fi

# On Stop/SessionEnd, retire .vendor_active ONLY if dismissal was confirmed by HTTP 200.
# If delivery timed out or failed, keep .vendor_active intact so Herdr reconciler retries.
if [ -n "$VENDOR_ACTIVE" ] && { [ "$EVENT" = "Stop" ] || [ "$EVENT" = "SessionEnd" ]; }; then
  if [ -n "$PREV_VENDOR_SID" ] && [ "$PREV_VENDOR_SID" != "$SID" ] && is_bartender_alive; then
    curl -s \
      --noproxy '*' \
      --max-redirs 0 \
      --proto =http \
      --connect-timeout 0.15 \
      --max-time 0.5 \
      -X POST "http://${HOST}:${PORT}/event" \
      -H 'Content-Type: application/json' \
      --data-raw "{\"state\":\"Ended\",\"agent\":\"Antigravity\",\"session_id\":\"${PREV_VENDOR_SID}\"}" >/dev/null 2>&1 || true
  fi
  if [ "$http_code" = "200" ]; then
    retire_vendor_file "$VENDOR_ACTIVE" "${PREV_VENDOR_SID:-$SID}"
  fi
fi

# Emit expected JSON response to stdout for Antigravity lifecycle
case "$EVENT" in
  PreToolUse) printf '{"decision":"allow"}\n' ;;
  Stop|SessionEnd) printf '{"decision":""}\n' ;;
  *) printf '{}\n' ;;
esac

exit 0
