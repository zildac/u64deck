#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
rm -f "$BIN_DIR/u64deck" "$DATA_HOME/applications/u64deck.desktop"
if [[ -f "$DATA_HOME/u64deck/install-path" ]] && \
   [[ "$(cat "$DATA_HOME/u64deck/install-path" 2>/dev/null || true)" == "$SCRIPT_DIR" ]]; then
  rm -f "$DATA_HOME/u64deck/install-path"
fi
echo "u64deck launchers removed."
echo "The source folder and your XDG configuration/data were not deleted."
