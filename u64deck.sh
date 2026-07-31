#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "u64deck is not installed in this folder." >&2
  echo "Run: $SCRIPT_DIR/install.sh" >&2
  exit 1
fi
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/u64deck"
mkdir -p "$STATE_HOME"
LOG_FILE="$STATE_HOME/u64deck.log"
exec > >(tee -a "$LOG_FILE") 2>&1
exec "$PYTHON" "$SCRIPT_DIR/linux/entry.py" "$@"
