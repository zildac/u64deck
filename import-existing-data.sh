#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/windows-or-older-linux-u64deck" >&2
  exit 2
fi
SOURCE="$(cd -- "$1" 2>/dev/null && pwd -P)" || {
  echo "Source directory not found: $1" >&2
  exit 1
}
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/u64deck"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/u64deck"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DATA_DIR/import-backups/$STAMP"
mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$BACKUP"
if [[ -f "${XDG_STATE_HOME:-$HOME/.local/state}/u64deck/u64deck.pid" ]]; then
  pid="$(cat "${XDG_STATE_HOME:-$HOME/.local/state}/u64deck/u64deck.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "u64deck is running (PID $pid). Use Exit u64deck before importing." >&2
    exit 1
  fi
fi
copied=0
backup_and_copy() {
  local src="$1" dst="$2"
  if [[ -e "$dst" ]]; then
    mkdir -p "$BACKUP/$(dirname -- "${dst#$DATA_DIR/}")"
    cp -a -- "$dst" "$BACKUP/$(basename -- "$dst")"
  fi
  mkdir -p "$(dirname -- "$dst")"
  cp -a -- "$src" "$dst"
  echo "Imported: $(basename -- "$src") -> $dst"
  copied=$((copied+1))
}
if [[ -f "$SOURCE/config.json" ]]; then
  if [[ -f "$CONFIG_DIR/config.json" ]]; then
    cp -a -- "$CONFIG_DIR/config.json" "$BACKUP/config.json"
  fi
  cp -a -- "$SOURCE/config.json" "$CONFIG_DIR/config.json"
  echo "Imported: config.json -> $CONFIG_DIR/config.json"
  copied=$((copied+1))
fi
shopt -s nullglob
candidates=(
  "$SOURCE/.u64deck-index.sqlite3"
  "$SOURCE"/.u64deck-index-*.sqlite3
  "$SOURCE/.sidflow-similarity.sqlite"
  "$SOURCE/user_items.json"
  "$SOURCE/playlists.json"
  "$SOURCE/.songlengths.cache"
  "$SOURCE/.espressif-ouis-cache.json"
  "$SOURCE/.imagecache.json"
  "$SOURCE/.dircache.json"
  "$SOURCE/.indexmeta.json"
  "$SOURCE/.legacy-cache-imported"
)
for src in "${candidates[@]}"; do
  [[ -f "$src" ]] || continue
  [[ "$src" == *-wal || "$src" == *-shm ]] && continue
  backup_and_copy "$src" "$DATA_DIR/$(basename -- "$src")"
done
for dir in library index-backups; do
  if [[ -d "$SOURCE/$dir" ]]; then
    if [[ -e "$DATA_DIR/$dir" ]]; then
      cp -a -- "$DATA_DIR/$dir" "$BACKUP/$dir"
      rm -rf -- "$DATA_DIR/$dir"
    fi
    cp -a -- "$SOURCE/$dir" "$DATA_DIR/$dir"
    echo "Imported directory: $dir"
    copied=$((copied+1))
  fi
done
WINDOWS_PATH_RE='(^|[^[:alnum:]+.-])[A-Za-z]:[\\/]'
if grep -RIlE "${WINDOWS_PATH_RE}|/Users/" \
     "$CONFIG_DIR/config.json" "$DATA_DIR"/*.json 2>/dev/null | grep -q .; then
  echo
  echo "Warning: imported JSON contains Windows paths. Update the affected paths in Settings."
fi
sqlite_files=("$DATA_DIR"/*.sqlite "$DATA_DIR"/*.sqlite3 "$DATA_DIR"/.*.sqlite "$DATA_DIR"/.*.sqlite3)
if ((${#sqlite_files[@]})) && command -v strings >/dev/null 2>&1 && \
   strings "${sqlite_files[@]}" 2>/dev/null | grep -Eq "$WINDOWS_PATH_RE"; then
  echo "Warning: an imported SQLite index contains Windows paths. Rebuild or remap that index for Linux-mounted folders."
fi
echo
if ((copied == 0)); then
  echo "No recognised persistent files were found in: $SOURCE"
else
  echo "Import complete. Source files were left untouched."
  echo "Backup of replaced Linux files: $BACKUP"
fi
