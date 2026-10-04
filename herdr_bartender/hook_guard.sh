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
        _HB_MARKER_MTIME=$(stat -c %Y "$_HB_PANE_MARKER" 2>/dev/null) || _HB_MARKER_MTIME=$(stat -f %m "$_HB_PANE_MARKER" 2>/dev/null) || _HB_MARKER_MTIME=0
        case "$_HB_MARKER_MTIME" in ''|*[!0-9]*) _HB_MARKER_MTIME=0 ;; esac
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
          if [ "$_HB_CAPTURE_ERR" -eq 0 ] && [ "$_HB_HERDR_HEALTHY" -eq 1 ]; then
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
# END HERDR-BARTENDER DEDUP GUARD
