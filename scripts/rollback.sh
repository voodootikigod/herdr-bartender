#!/bin/bash
# Deterministic rollback of the herdr-bartender integration (Plan §9.1).
#
# Adapted from the plan block:
#   - the launcher is resolved relative to this script (<repo>/bin/herdr-bartender),
#     overridable with HERDR_BARTENDER_BIN;
#   - both plugin link locations are removed (README path plugins/local/...);
#   - state dir follows the §2.2 rule; vendor hooks dir honours
#     HERDR_BARTENDER_VENDOR_HOOKS_DIR like the Python code;
#   - pkill is scoped to the current user;
#   - the fallback guard strip is the byte-exact inverse of the installer.
# Exit 0: everything removed. Exit 1: something needs attention; DISABLED stays.
set -u
umask 077

ERRORS=0
STATE_DIR="${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"
VENDOR_HOOKS_DIR="${HERDR_BARTENDER_VENDOR_HOOKS_DIR:-$HOME/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks}"
ORPHAN_FILE="$HOME/.herdr-bartender-orphans.json"
PLUGIN_LINKS=("$HOME/.config/herdr/plugins/herdr-bartender" "$HOME/.config/herdr/plugins/local/herdr-bartender")
RECONCILER_PATTERN="herdr-bartender --reconcile-background"

# Resolve this script's real directory (follows symlinks without GNU readlink -f).
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir=$(cd -P "$(dirname "$_src")" && pwd)
  _src=$(readlink "$_src")
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SCRIPT_DIR=$(cd -P "$(dirname "$_src")" && pwd)
BIN="${HERDR_BARTENDER_BIN:-$(dirname "$SCRIPT_DIR")/bin/herdr-bartender}"

case "${STATE_DIR%/}" in
  "" | "/" | "${HOME%/}")
    echo "Error: refusing to use unsafe state directory '$STATE_DIR'"
    exit 1
    ;;
esac

stop_reconcilers() {
  pkill -9 -u "$(id -u)" -f "$RECONCILER_PATTERN" 2>/dev/null || true
}

# Byte-exact inverse of the installer's insert (herdr_bartender/hooks_text.py).
strip_guard_py() {
  python3 -c '
import os, subprocess, sys
B = b"# BEGIN HERDR-BARTENDER DEDUP GUARD"
E = b"# END HERDR-BARTENDER DEDUP GUARD"
hook = sys.argv[1]
mode = os.stat(hook).st_mode & 0o7777
with open(hook, "rb") as f:
    data = f.read()
if data.count(B) != 1 or data.count(E) != 1:
    sys.exit(3)
start, end_at = data.index(B), data.index(E)
if end_at < start or (start and data[start - 1:start] != b"\n") or data[end_at - 1:end_at] != b"\n":
    sys.exit(3)
end = end_at + len(E)
tail = data[end:end + 1]
if tail not in (b"", b"\n"):
    sys.exit(3)
cleaned = data[:start] + data[end + len(tail):]
tmp = "%s.tmp.%d" % (hook, os.getpid())
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(cleaned)
    os.chmod(tmp, mode)
    if subprocess.run(["bash", "-n", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        sys.exit(1)
    with open(hook, "rb") as f:
        if f.read() != data:
            sys.exit(1)
    os.replace(tmp, hook)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
' "$1"
}

strip_hooks_fallback() {
  local hook begin_count end_count rc
  for hook in "$VENDOR_HOOKS_DIR/claude-event-hook.sh" "$VENDOR_HOOKS_DIR/codex-notify-hook.sh"; do
    [ -f "$hook" ] || continue
    begin_count=$(grep -c "# BEGIN HERDR-BARTENDER DEDUP GUARD" "$hook" 2>/dev/null || true)
    end_count=$(grep -c "# END HERDR-BARTENDER DEDUP GUARD" "$hook" 2>/dev/null || true)
    if [ "${begin_count:-0}" -eq 0 ] && [ "${end_count:-0}" -eq 0 ]; then
      continue
    fi
    rc=0
    if [ "$begin_count" -eq 1 ] && [ "$end_count" -eq 1 ]; then
      strip_guard_py "$hook" 2>/dev/null || rc=$?
    else
      rc=3
    fi
    if [ "$rc" -eq 3 ]; then
      echo "Warning: mismatched dedup markers in $hook; manual inspection required"
      ERRORS=$((ERRORS + 1))
    elif [ "$rc" -ne 0 ]; then
      echo "Error: failed to strip $hook"
      ERRORS=$((ERRORS + 1))
    fi
  done
}

# Step 0: tombstone so normal event handlers immediately no-op
if ! mkdir -p -m 700 "$STATE_DIR" || ! touch "$STATE_DIR/DISABLED"; then
  echo "Error: cannot create tombstone $STATE_DIR/DISABLED; aborting"
  exit 1
fi

# Step 1: remove plugin links and verify Herdr no longer lists the plugin
for link in "${PLUGIN_LINKS[@]}"; do
  if [ -L "$link" ]; then
    rm -f "$link" || { echo "Error: failed to remove plugin link $link"; ERRORS=$((ERRORS + 1)); }
  elif [ -e "$link" ]; then
    echo "Warning: $link is not a symlink; remove it manually"
    ERRORS=$((ERRORS + 1))
  fi
done
if command -v herdr >/dev/null 2>&1; then
  herdr plugin unlink herdr-bartender >/dev/null 2>&1 || true
  if herdr plugin list 2>/dev/null | grep -q "herdr-bartender"; then
    echo "Error: Failed to unlink herdr-bartender plugin from Herdr; keeping tombstone active to prevent events"
    exit 1
  fi
fi

# Step 2: terminate running background reconcilers (this user only)
stop_reconcilers

# Step 3: clear Top Shelf entries (--cleanup bypasses the tombstone and has its own budget)
if [ -x "$BIN" ]; then
  CLEANUP_EXIT=0
  "$BIN" --cleanup 2>/dev/null || CLEANUP_EXIT=$?
  if [ "$CLEANUP_EXIT" -eq 2 ]; then
    echo "Notice: Bartender bridge unreachable during cleanup; active sessions exported to $ORPHAN_FILE"
    ERRORS=$((ERRORS + 1))
  elif [ "$CLEANUP_EXIT" -ne 0 ]; then
    echo "Warning: Fatal error during cleanup (exit code $CLEANUP_EXIT)"
    ERRORS=$((ERRORS + 1))
  fi
else
  echo "Warning: $BIN is not executable; Top Shelf cleanup skipped"
  ERRORS=$((ERRORS + 1))
fi

# Step 4: strip the delimited guard block from the named vendor hooks
if [ -x "$BIN" ]; then
  "$BIN" --uninstall-hooks 2>/dev/null || ERRORS=$((ERRORS + 1))
else
  strip_hooks_fallback
fi

# Step 5: remove the state dir ONLY if cleanup was confirmed (exit 0) and every step succeeded
if [ "$ERRORS" -eq 0 ]; then
  stop_reconcilers
  if ! rm -rf "$STATE_DIR"; then
    echo "Error: failed to remove $STATE_DIR"
    exit 1
  fi
  echo "Rollback completed successfully."
  exit 0
fi
echo "Rollback finished with $ERRORS warning(s)/error(s). Keeping $STATE_DIR/DISABLED active."
if [ -f "$ORPHAN_FILE" ]; then
  echo "When Bartender 6 is running, replay orphaned sessions via:"
  echo "  $BIN --replay-orphans $ORPHAN_FILE"
fi
exit 1
