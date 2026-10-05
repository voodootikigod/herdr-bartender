# BEGIN HERDR-BARTENDER DEDUP GUARD
if [ -n "${HERDR_PANE_ID:-}" ]; then
  if printf '%s' "$HERDR_PANE_ID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
    _HB_CANONICAL_PANE=""
    if printf '%s' "$HERDR_PANE_ID" | grep -q ':'; then
      _HB_CANONICAL_PANE="$HERDR_PANE_ID"
    elif [ -n "${HERDR_WORKSPACE_ID:-}" ]; then
      _HB_CANONICAL_PANE="${HERDR_WORKSPACE_ID}:${HERDR_PANE_ID}"
    else
      _HB_CANONICAL_PANE=""
    fi
    if printf '%s' "$_HB_CANONICAL_PANE" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'; then
      _HB_STATE_HOME="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
      _HB_HEX_PANE=$(printf '%s' "$_HB_CANONICAL_PANE" | LC_ALL=C od -An -v -tx1 | tr -d ' \t\n')
      _HB_PANE_MARKER="$_HB_STATE_HOME/panes/${_HB_HEX_PANE}"
      _HB_VENDOR_ACTIVE="$_HB_STATE_HOME/panes/${_HB_HEX_PANE}.vendor_active"
      # R16: every file the guard writes is private; caller umask restored before the block ends.
      _HB_OLD_UMASK=$(umask)
      umask 077

      _HB_IS_SESSION_TERMINAL=0
      _HB_IS_TURN_TERMINAL=0
      _HB_ARGV_IS_JSON=0
      _HB_ARGV_SID=""
      _HB_CLASSIFY_ERR=0
      # R36: classify by TOP-LEVEL members only (hook_event_name/event/state/type and session_id), never by keys
      # nested in tool_input/tool_response. Prints "<S|T|N> <session_id>": S session-terminal, T turn-terminal,
      # N neither; no output fails open. R41: one streaming pass, linear in the payload under BWK awk (macOS), mawk
      # and busybox: lines are split on '"' (a string whose quote has an odd backslash run continues), escape pairs
      # are neutralised, and brackets are found with nested single-character splits (no per-match gsub/substr over
      # the payload, which is quadratic in BWK awk). Lines keep BWK's split from treating newlines as separators.
      _HB_AWK='function sess(v) { return v == "Ended" || v == "SessionEnd" || v == "session-end" }
function turn(v) { return v == "Stop" || v == "Done" || v == "AgentDone" || v == "AgentWaiting" || v == "agent-turn-complete" }
function oddtail(x,   n, j) {
  n = length(x)
  if (substr(x, n, 1) != "\\") return 0
  if (substr(x, n - 1, 1) != "\\") return 1
  n = split(x, Y, "\\"); j = n
  while (j > 1 && Y[j] == "") j--
  return (n - j) % 2
}
function keep(x) { if (length(a) <= 256) a = a substr(x, 1, 257) }
function top(u) {
  if (match(u, /[,:][^,:]*$/)) { w = (substr(u, RSTART, 1) == ",") ? "k" : "v"; u = substr(u, RSTART + 1) }
  if (w == "v" && u ~ /[^ \t\r\n]/) w = ""
}
function br(c) {
  if (c == "{" || c == "[") { d++; if (d == 1) { o = (c == "{"); w = "k" } else w = "" }
  else if (--d < 1) z = 1
}
function seg(x,   na, ia, nb, ib, nc, ic, ne, ie) {
  if (index(x, "\\")) gsub(/\\./, "\002", x)
  if (!index(x, "{") && !index(x, "}") && !index(x, "[") && !index(x, "]")) { if (d == 1) top(x); return }
  na = split(x, A, "]")
  for (ia = 1; ia <= na && !z; ia++) {
    if (ia > 1) { br("]"); if (z) break }
    nb = split(A[ia], B, "}")
    for (ib = 1; ib <= nb && !z; ib++) {
      if (ib > 1) { br("}"); if (z) break }
      nc = split(B[ib], C, "[")
      for (ic = 1; ic <= nc; ic++) {
        if (ic > 1) br("[")
        ne = split(C[ic], E, "{")
        for (ie = 1; ie <= ne; ie++) {
          if (ie > 1) br("{")
          if (d == 1) top(E[ie])
        }
      }
    }
  }
}
function endstr() {
  gsub(/\\./, "\002", a)
  if (r == "k") { k = a; w = "" }
  else if (r == "v") { if (!(k in t)) t[k] = a; w = "" }
  q = 0; r = ""
}
BEGIN { d = 0; w = ""; k = ""; o = 0; q = 0; z = 0; r = ""; a = ""; nl = 0; h = 0 }
z || h { next }
{
  s = $0
  if ((i = index(s, "\001")) > 0) { s = substr(s, 1, i - 1); h = 1 }
  if (q && nl && r != "") keep("\n")
  nl = 0
  n = split(s, p, "\"")
  for (i = 1; i <= n && !z; i++) {
    if (q) {
      if (r != "") keep(p[i])
      if (i == n) nl = 1
      else if (oddtail(p[i])) { if (r != "") keep("\"") }
      else endstr()
    } else if (i < n && oddtail(p[i])) seg(p[i] "\"")
    else {
      seg(p[i])
      if (i < n && !z) { q = 1; r = (o && d == 1) ? w : ""; a = "" }
    }
  }
}
END {
  if (q) endstr()
  v = "N"
  split("hook_event_name event state type", K, " ")
  for (j = 1; j <= 4; j++) if ((K[j] in t) && sess(t[K[j]])) v = "S"
  if (v == "N") for (j = 1; j <= 4; j++) if ((K[j] in t) && turn(t[K[j]])) v = "T"
  print v " " (("session_id" in t) ? t["session_id"] : "")
}'
      # R41: the classifier runs under its own 1s deadline (perl's alarm survives the exec into awk, which the default
      # SIGALRM action then ends); a timeout is a classification error and fails open (pass through).
      _HB_HAS_PERL=0
      if command -v perl >/dev/null 2>&1; then _HB_HAS_PERL=1; fi
      _HB_DEADLINE='$SIG{ALRM} = "DEFAULT"; alarm 1; exec { $ARGV[0] } @ARGV; exit 127;'

      case "${1:-}" in
        "{"*)
          _HB_ARGV_IS_JSON=1
          if [ "$_HB_HAS_PERL" -eq 1 ]; then
            _HB_CLASS=$(printf '%s' "$1" | LC_ALL=C perl -e "$_HB_DEADLINE" awk "$_HB_AWK" 2>/dev/null) || _HB_CLASS=""
          else
            _HB_CLASS=$(printf '%s' "$1" | LC_ALL=C awk "$_HB_AWK" 2>/dev/null) || _HB_CLASS=""
          fi
          case "$_HB_CLASS" in
            S*) _HB_IS_SESSION_TERMINAL=1 ;;
            T*) _HB_IS_TURN_TERMINAL=1 ;;
            N*) : ;;
            *) _HB_CLASSIFY_ERR=1 ;;
          esac
          _HB_RAW_SID="${_HB_CLASS#??}"
          # grep matches line by line: a multi-line value (raw newline in the JSON string) is never a UUID.
          case "$_HB_RAW_SID" in *"
"*) _HB_RAW_SID="" ;; esac
          if printf '%s' "$_HB_RAW_SID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
            _HB_ARGV_SID="$_HB_RAW_SID"
          fi
          ;;
        Ended|SessionEnd|session-end) _HB_IS_SESSION_TERMINAL=1 ;;
        Stop|Done|AgentDone|AgentWaiting|agent-turn-complete) _HB_IS_TURN_TERMINAL=1 ;;
      esac

      _HB_GUARD_TMP=""
      _HB_CAPTURE_ERR=0
      if [ "$_HB_IS_SESSION_TERMINAL" -eq 0 ]; then
        if [ ! -t 0 ]; then
          mkdir -m 700 -p "$_HB_STATE_HOME" 2>/dev/null || true
          _HB_GUARD_TMP=$(mktemp "$_HB_STATE_HOME/.guard_stdin.XXXXXX" 2>/dev/null || true)
          if [ -n "$_HB_GUARD_TMP" ]; then
            # A capture that cannot write what it read (disk full) exits 75 after forking a splicer that feeds
            # <capture>.fifo with the file's prefix, the unwritten bytes and the rest of stdin (then removes the file).
            # The splicer gives up after 30s if the guard never opens the FIFO (the hook was killed).
            if [ "$_HB_HAS_PERL" -eq 1 ]; then
              # R41: a cooperative 1s deadline (select + sysread + syswrite, no signal handler): every chunk taken
              # from the pipe is written in full before the deadline is checked again, so a timeout never drops one.
              perl -e 'use Time::HiRes qw(time); my $end = time + 1; my $in = ""; vec($in, 0, 1) = 1;
sub splice_rest {
  my ($rest, $tmp) = ($_[0], $ARGV[0]); my $fifo = "$tmp.fifo"; my $data;
  open(my $kept, "<", $tmp); require POSIX; exit 1 unless POSIX::mkfifo($fifo, 0600);
  my $pid = fork; if (!defined $pid) { unlink $fifo; exit 1 } exit 75 if $pid;
  alarm 30; open(my $out, ">", $fifo) or exit 1; alarm 0; unlink $tmp;
  if ($kept) { print {$out} $data while read($kept, $data, 65536) }
  print {$out} $rest;
  while (1) { my $got = sysread(STDIN, $data, 65536); if (!defined $got) { next if $!{EINTR}; last } last if !$got; print {$out} $data }
  exit 0;
}
while (1) {
  my $left = $end - time; exit 142 if $left <= 0;
  my $n = select(my $ready = $in, undef, undef, $left);
  if ($n < 0) { next if $!{EINTR}; exit 142 }
  exit 142 if $n == 0;
  my $got = sysread(STDIN, my $buf, 65536);
  if (!defined $got) { next if $!{EINTR} || $!{EAGAIN}; exit 142 }
  last if $got == 0;
  for (my $off = 0; $off < $got; ) {
    my $put = syswrite(STDOUT, $buf, $got - $off, $off);
    if (!defined $put) { next if $!{EINTR}; splice_rest(substr($buf, $off, $got - $off)) }
    $off += $put;
  }
}
exit 0;' "$_HB_GUARD_TMP" > "$_HB_GUARD_TMP" 2>/dev/null || _HB_CAPTURE_ERR=$?
            elif command -v python3 >/dev/null 2>&1; then
              # Unbuffered os.read + select deadline (no signal): every chunk read is written before the 1s
              # deadline is checked again, so a timeout never swallows bytes already taken from the pipe.
              python3 -c 'import os, select, sys, time
def splice_rest(rest):
    tmp = sys.argv[1]
    fifo = tmp + ".fifo"
    try:
        kept = open(tmp, "rb")
    except OSError:
        kept = None
    try:
        os.mkfifo(fifo, 0o600)
    except OSError:
        os._exit(1)
    try:
        pid = os.fork()
    except OSError:
        os.unlink(fifo)
        os._exit(1)
    if pid:
        os._exit(75)
    import signal
    signal.alarm(30)
    out = os.open(fifo, os.O_WRONLY)
    signal.alarm(0)
    try:
        os.unlink(tmp)
        def put(data):
            while data:
                data = data[os.write(out, data):]
        for block in iter(lambda: kept.read(65536) if kept else b"", b""):
            put(block)
        put(rest)
        for block in iter(lambda: os.read(0, 65536), b""):
            put(block)
    except OSError:
        pass
    os._exit(0)
end = time.monotonic() + 1.0
while True:
    left = end - time.monotonic()
    if left <= 0 or not select.select([0], [], [], left)[0]:
        os._exit(142)
    chunk = os.read(0, 65536)
    if not chunk:
        break
    while chunk:
        try:
            chunk = chunk[os.write(1, chunk):]
        except OSError:
            splice_rest(chunk)' "$_HB_GUARD_TMP" > "$_HB_GUARD_TMP" 2>/dev/null || _HB_CAPTURE_ERR=$?
            else
              # Pure bash cannot safely capture unbounded multiline stdin with timeout.
              # Skip stdin capture and leave stdin untouched for the vendor script.
              rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
              _HB_GUARD_TMP=""
              _HB_CAPTURE_ERR=1
            fi
            if [ "$_HB_CAPTURE_ERR" -eq 75 ] && [ -p "${_HB_GUARD_TMP}.fifo" ]; then
              # The capture's splicer replays every byte through the FIFO (and removes the capture file).
              _HB_SPLICE_FIFO="${_HB_GUARD_TMP}.fifo"
              exec < "$_HB_SPLICE_FIFO" || true
              rm -f "$_HB_SPLICE_FIFO" 2>/dev/null || true
              _HB_GUARD_TMP=""
            elif [ "$_HB_CAPTURE_ERR" -ne 0 ]; then
              if [ -s "$_HB_GUARD_TMP" ]; then
                _HB_SPLICE_FIFO=$(mktemp -u "$_HB_STATE_HOME/.guard_splice.XXXXXX" 2>/dev/null || true)
                if [ -n "$_HB_SPLICE_FIFO" ] && mkfifo "$_HB_SPLICE_FIFO" 2>/dev/null; then
                  # An async list gets /dev/null as stdin in POSIX shells (dash ignores a `<&0` override),
                  # so hand the real stdin over on fd 9, scoped to the group so the caller's fd 9 is untouched.
                  { ( cat "$_HB_GUARD_TMP"; rm -f "$_HB_GUARD_TMP" 2>/dev/null || true; exec cat <&9 9<&- ) > "$_HB_SPLICE_FIFO" 2>/dev/null & } 9<&0
                  exec < "$_HB_SPLICE_FIFO" || true
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
        if [ "$_HB_HAS_PERL" -eq 1 ]; then
          _HB_CLASS=$(LC_ALL=C perl -e "$_HB_DEADLINE" awk "$_HB_AWK" "$_HB_GUARD_TMP" 2>/dev/null) || _HB_CLASS=""
        else
          _HB_CLASS=$(LC_ALL=C awk "$_HB_AWK" "$_HB_GUARD_TMP" 2>/dev/null) || _HB_CLASS=""
        fi
        case "$_HB_CLASS" in
          S*) _HB_IS_SESSION_TERMINAL=1 ;;
          T*) _HB_IS_TURN_TERMINAL=1 ;;
          N*) : ;;
          *) _HB_CLASSIFY_ERR=1 ;;
        esac
        _HB_RAW_SID="${_HB_CLASS#??}"
        case "$_HB_RAW_SID" in *"
"*) _HB_RAW_SID="" ;; esac
        if printf '%s' "$_HB_RAW_SID" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_-]{16,64}$'; then
          _HB_VENDOR_SID="$_HB_RAW_SID"
        fi
      fi
      # An unclassifiable payload (awk missing or failed) is never suppressed: fail open.
      if [ "$_HB_CLASSIFY_ERR" -ne 0 ]; then
        _HB_CAPTURE_ERR=1
      fi

      # R74/R75: a genuine .vendor_active is only ever a small regular file written by mktemp + mv. It is read ONCE,
      # without following symlinks or blocking, and at most 4097 bytes (exit 0 read, 1 missing, 3 unusable: a
      # symlink, non-regular or over 4 KiB). An unusable entry is removed (rm never follows); one rm cannot remove
      # (a directory) blocks every .vendor_active write and simply passes through. Decisions use the bytes read.
      _HB_VA_BLOCKED=0
      _HB_VA_CONTENT=""
      if [ "$_HB_HAS_PERL" -eq 1 ]; then
        if _HB_VA_CONTENT=$(perl -e 'use Fcntl; my $p = shift;
            sysopen(my $f, $p, O_RDONLY|O_NONBLOCK|O_NOFOLLOW) or exit($!{ENOENT} ? 1 : 3);
            stat($f); -f _ or exit 3; my $n = sysread($f, my $b, 4097);
            exit 3 if !defined $n || $n > 4096; print $b; exit 0' "$_HB_VENDOR_ACTIVE" 2>/dev/null); then
          _HB_VA_STATE=0
        else
          _HB_VA_STATE=$?
        fi
      elif command -v python3 >/dev/null 2>&1; then
        if _HB_VA_CONTENT=$(python3 -c 'import os, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
except FileNotFoundError:
    sys.exit(1)
except OSError:
    sys.exit(3)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    sys.exit(3)
b = os.read(fd, 4097)
if len(b) > 4096:
    sys.exit(3)
sys.stdout.buffer.write(b)' "$_HB_VENDOR_ACTIVE" 2>/dev/null); then
          _HB_VA_STATE=0
        else
          _HB_VA_STATE=$?
        fi
      elif [ -L "$_HB_VENDOR_ACTIVE" ] || { [ -e "$_HB_VENDOR_ACTIVE" ] && [ ! -f "$_HB_VENDOR_ACTIVE" ]; }; then
        _HB_VA_STATE=3
      elif [ ! -e "$_HB_VENDOR_ACTIVE" ]; then
        _HB_VA_STATE=1
      else
        _HB_VA_CONTENT=$(head -c 4097 "$_HB_VENDOR_ACTIVE" 2>/dev/null) || _HB_VA_CONTENT=""
        _HB_VA_STATE=0
        if [ "${#_HB_VA_CONTENT}" -gt 4096 ]; then _HB_VA_STATE=3; fi
      fi
      case "$_HB_VA_STATE" in
        0|1) ;;
        *)
          _HB_VA_CONTENT=""
          rm -f "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
          if [ -L "$_HB_VENDOR_ACTIVE" ] || [ -e "$_HB_VENDOR_ACTIVE" ]; then
            _HB_VA_BLOCKED=1
          fi
          ;;
      esac

      _HB_VA_HAS_UUID=0
      if [ "$_HB_VA_BLOCKED" -eq 0 ] && [ -n "$_HB_VA_CONTENT" ] \
         && printf '%s' "$_HB_VA_CONTENT" | grep -m1 -Eq '\{"vendor_session_id":' 2>/dev/null; then
        _HB_VA_HAS_UUID=1
      fi

      _HB_HERDR_HEALTHY=0
      # R73: a flag counts as set if ANY entry exists at its path (a planted or dangling symlink fails open); the
      # marker must be a regular file, never a symlink (its target's mtime proves nothing about Herdr).
      if [ ! -e "$_HB_STATE_HOME/DISABLED" ] && [ ! -L "$_HB_STATE_HOME/DISABLED" ] \
         && [ ! -e "$_HB_STATE_HOME/DELIVERY_DOWN" ] && [ ! -L "$_HB_STATE_HOME/DELIVERY_DOWN" ] \
         && [ -f "$_HB_PANE_MARKER" ] && [ ! -L "$_HB_PANE_MARKER" ] \
         && [ ! -e "${_HB_PANE_MARKER}.failed" ] && [ ! -L "${_HB_PANE_MARKER}.failed" ]; then
        _HB_MARKER_MTIME=$(stat -c %Y "$_HB_PANE_MARKER" 2>/dev/null) || _HB_MARKER_MTIME=$(stat -f %m "$_HB_PANE_MARKER" 2>/dev/null) || _HB_MARKER_MTIME=0
        case "$_HB_MARKER_MTIME" in ''|*[!0-9]*) _HB_MARKER_MTIME=0 ;; esac
        _HB_NOW_TIME=$(date +%s 2>/dev/null) || _HB_NOW_TIME=0
        case "$_HB_NOW_TIME" in ''|*[!0-9]*) _HB_NOW_TIME=0 ;; esac
        # R15: exact process name, or an executable inside the app bundle (never a loose cmdline match).
        # R44: -a, because macOS pgrep skips the caller's ancestors and Herdr is an ancestor of this hook.
        # Fresh only when both clocks are known and 0 <= age < 60; a failed `date`/`stat` or a
        # future (skewed) mtime is "unknown freshness" and must fail open (pass through).
        _HB_MARKER_AGE=-1
        if [ "$_HB_NOW_TIME" -gt 0 ] && [ "$_HB_MARKER_MTIME" -gt 0 ]; then
          _HB_MARKER_AGE=$((_HB_NOW_TIME - _HB_MARKER_MTIME))
        fi
        if [ "$_HB_MARKER_AGE" -ge 0 ] && [ "$_HB_MARKER_AGE" -lt 60 ] && \
           { pgrep -a -xi "herdr" >/dev/null 2>&1 || pgrep -a -f '^[^[:space:]]*/Herdr\.app/Contents/MacOS/' >/dev/null 2>&1; }; then
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
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP" 2>/dev/null || true
            mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || rm -f "$_HB_VA_TMP" 2>/dev/null || true
          fi   # R74: no direct (symlink-following) write when mktemp fails: pass through unrecorded
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
              printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP" 2>/dev/null || true
              mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || rm -f "$_HB_VA_TMP" 2>/dev/null || true
            fi   # R74: no direct (symlink-following) write when mktemp fails: pass through unrecorded
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
        if [ "$_HB_VA_BLOCKED" -eq 1 ]; then
          : # R74: an entry rm could not remove sits at .vendor_active: pass through unrecorded
        elif [ -n "$_HB_VENDOR_SID" ]; then
          _HB_VA_TMP=$(mktemp "$_HB_STATE_HOME/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
          if [ -n "$_HB_VA_TMP" ]; then
            printf '{"vendor_session_id":"%s"}' "$_HB_VENDOR_SID" > "$_HB_VA_TMP" 2>/dev/null || true
            mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || rm -f "$_HB_VA_TMP" 2>/dev/null || true
          fi   # R74: no direct (symlink-following) write when mktemp fails: pass through unrecorded
        else
          _HB_VA_TMP=$(mktemp "$_HB_STATE_HOME/panes/.va.tmp.XXXXXX" 2>/dev/null || true)
          if [ -n "$_HB_VA_TMP" ]; then   # R74: a bare record via mktemp + mv, never a symlink-following touch
            mv -f "$_HB_VA_TMP" "$_HB_VENDOR_ACTIVE" 2>/dev/null || rm -f "$_HB_VA_TMP" 2>/dev/null || true
          fi
        fi
      fi


      # Refresh only (-c never creates): Python may retire .vendor_active between the test and the touch.
      if [ "$_HB_IS_SESSION_TERMINAL" -eq 0 ] && [ -f "$_HB_VENDOR_ACTIVE" ] && [ ! -L "$_HB_VENDOR_ACTIVE" ]; then
        touch -c "$_HB_VENDOR_ACTIVE" 2>/dev/null || true
      fi
      if [ -n "$_HB_GUARD_TMP" ] && [ -f "$_HB_GUARD_TMP" ]; then
        exec < "$_HB_GUARD_TMP" || true
        rm -f "$_HB_GUARD_TMP" 2>/dev/null || true
      fi
      umask "$_HB_OLD_UMASK" 2>/dev/null || true
      unset _HB_CANONICAL_PANE _HB_HEX_PANE _HB_STATE_HOME _HB_PANE_MARKER _HB_VENDOR_ACTIVE \
            _HB_IS_SESSION_TERMINAL _HB_IS_TURN_TERMINAL _HB_VENDOR_SID _HB_VA_HAS_UUID \
            _HB_HERDR_HEALTHY _HB_MARKER_MTIME _HB_MARKER_AGE _HB_NOW_TIME _HB_OLD_UMASK _HB_FIRST_LINE \
            _HB_READ_STATUS _HB_RAW_SID _HB_VA_TMP _HB_CAPTURE_ERR _HB_GUARD_TMP _HB_SPLICE_FIFO \
            _HB_ARGV_IS_JSON _HB_ARGV_SID _HB_AWK _HB_CLASS _HB_CLASSIFY_ERR _HB_HAS_PERL _HB_DEADLINE \
            _HB_VA_BLOCKED _HB_VA_CONTENT _HB_VA_STATE
    fi
  fi
fi
# END HERDR-BARTENDER DEDUP GUARD
