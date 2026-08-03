#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="$SCRIPT_DIR/.venv"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/u64deck"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/u64deck"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/u64deck"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/u64deck"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 was not found." >&2
  echo "Ubuntu 24.04: sudo apt install python3 python3-venv" >&2
  exit 1
fi
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"u64deck requires Python 3.10 or newer; found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]}")
PY
mkdir -p "$DATA_DIR" "$STATE_DIR" "$CACHE_DIR" "$CONFIG_DIR" "$BIN_DIR" "$APP_DIR"
venv_args=()
if [[ "${U64DECK_INSTALL_SYSTEM_SITE_PACKAGES:-0}" == "1" ]]; then
  venv_args+=(--system-site-packages)
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  rm -rf "$VENV"
  if ! "$PYTHON_BIN" -m venv "${venv_args[@]}" "$VENV"; then
    echo "Could not create the local virtual environment." >&2
    echo "Ubuntu/Debian: sudo apt install python3-venv" >&2
    exit 1
  fi
fi
if [[ -n "${U64DECK_VALIDATION_SITE_PACKAGES:-}" ]]; then
  site_dir="$("$VENV/bin/python" - <<'PY_SITE'
import site
print(site.getsitepackages()[0])
PY_SITE
)"
  printf '%s\n' "$U64DECK_VALIDATION_SITE_PACKAGES" > "$site_dir/u64deck-validation.pth"
fi
if [[ "${U64DECK_INSTALL_SKIP_PIP:-0}" != "1" ]]; then
  "$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip
  "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$SCRIPT_DIR/requirements.txt"
else
  "$VENV/bin/python" - <<'PY'
import fastapi, uvicorn, httpx, multipart, psutil
print("Using matching preinstalled Python dependencies for validation.")
PY
fi
chmod +x "$SCRIPT_DIR/u64deck.sh" "$SCRIPT_DIR/update-linux.sh" \
  "$SCRIPT_DIR/uninstall-linux.sh" "$SCRIPT_DIR/import-existing-data.sh"

cat > "$BIN_DIR/u64deck" <<EOF
#!/usr/bin/env bash
exec "$SCRIPT_DIR/u64deck.sh" "\$@"
EOF
chmod +x "$BIN_DIR/u64deck"
cat > "$APP_DIR/u64deck.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=u64deck Linux Preview 7
Comment=Ultimate 64 control deck
Exec="$SCRIPT_DIR/u64deck.sh"
Icon=$SCRIPT_DIR/u64deck.ico
Terminal=false
Categories=Game;Utility;
StartupNotify=true
EOF
chmod 644 "$APP_DIR/u64deck.desktop"
printf '%s\n' "$SCRIPT_DIR" > "$DATA_DIR/install-path"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Note: $BIN_DIR is not currently on PATH; the application-menu entry still works."
fi
runtime_files=()
for name in config.json .u64deck-index.sqlite3 .sidflow-similarity.sqlite user_items.json playlists.json; do
  [[ -e "$SCRIPT_DIR/$name" ]] && runtime_files+=("$name")
done
if ((${#runtime_files[@]})); then
  echo
  echo "Existing runtime files were found beside the Linux scripts: ${runtime_files[*]}"
  echo "Linux does not read them there. Import them with:"
  echo "  $SCRIPT_DIR/import-existing-data.sh '$SCRIPT_DIR'"
fi
echo
echo "u64deck Linux Preview 7 installed in: $SCRIPT_DIR"
echo "Launch with: $SCRIPT_DIR/u64deck.sh"
echo "Configuration: $CONFIG_DIR/config.json"
echo "Indexes, SIDFlow, favourites and playlists: $DATA_DIR"
