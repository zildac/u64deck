#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/u64deck"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/u64deck"
META="$DATA_DIR/install-path"

if [[ "${1:-}" == "--rollback" ]]; then
  [[ $# -eq 2 ]] || { echo "Usage: $0 --rollback /path/to/previous/u64deck" >&2; exit 2; }
  TARGET="$(cd -- "$2" && pwd -P)"
  [[ -x "$TARGET/u64deck.sh" && -x "$TARGET/.venv/bin/python" ]] || {
    echo "Rollback target is not an installed u64deck folder: $TARGET" >&2; exit 1; }
  mkdir -p "$DATA_DIR"
  printf '%s\n' "$TARGET" > "$META"
  "$TARGET/install.sh" >/dev/null
  echo "Linux launcher rolled back to: $TARGET"
  exit 0
fi

if [[ -f "$STATE_DIR/u64deck.pid" ]]; then
  pid="$(cat "$STATE_DIR/u64deck.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "u64deck is running (PID $pid). Use Exit u64deck before updating." >&2
    exit 1
  fi
fi

OLD=""
[[ -f "$META" ]] && OLD="$(cat "$META" 2>/dev/null || true)"
if [[ -n "$OLD" && "$OLD" != "$SCRIPT_DIR" && -d "$OLD" ]]; then
  echo "Previous installation: $OLD"
  if [[ -e "$OLD/config.json" || -e "$OLD/.u64deck-index.sqlite3" || \
        -e "$OLD/.sidflow-similarity.sqlite" || -e "$OLD/user_items.json" ]]; then
    echo "Importing older in-folder state into the Linux XDG locations..."
    "$SCRIPT_DIR/import-existing-data.sh" "$OLD"
  fi
fi

old_meta="$OLD"
restore_meta() {
  if [[ -n "$old_meta" ]]; then
    printf '%s\n' "$old_meta" > "$META"
  else
    rm -f "$META"
  fi
}
trap 'restore_meta' ERR
"$SCRIPT_DIR/install.sh"
trap - ERR

echo
echo "Update complete. Persistent XDG data was retained."
[[ -n "$OLD" && "$OLD" != "$SCRIPT_DIR" ]] && echo "Previous folder left untouched: $OLD"
echo "Current folder: $SCRIPT_DIR"
