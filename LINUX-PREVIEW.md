# u64deck v1.9.0 — Linux Preview 7

Linux Preview 7 is the source-run Linux distribution matched to the reviewed
Windows v1.9.0 RC48 core (`c0d1fb0`). It is published in the same `zildac/u64deck`
repository. No frozen ELF or AppImage is produced.

Windows RC48 and Linux Preview 7 are matched soak candidates. The Linux preview
is provided as-is for testing: expect rough edges, include the full Linux
version/build identity in reports, and state the distribution, desktop session,
Python version and browser package used.

## Tested platform

The earlier Linux Preview 4 hardware-tested environment was Ubuntu 24.04.4 LTS on amd64, kernel
6.8.0-136-generic, Ubuntu GNOME under Wayland, Python 3.12.3 and Chromium
150.0.7871.128 from Canonical's stable Snap channel. Linux Preview 7 retains that architecture but has not yet been run on the maintainer NUC.

That earlier hardware validation covered installation and application-menu launch, Chromium
app-window mode, Ultimate discovery and connection, screen mirror at about
50 fps, HDMI audio, fullscreen, recording, Legacy physical-F7 guidance,
SIDFlow download/verify/import, local SID/HVSC and storage indexing, SID Search,
SIDFlow recommendations, SID playback, Storage browsing, Mount & Run,
upgrade-state retention and clean Exit.

## Install

Chromium must be installed separately. On the tested Ubuntu platform:

```bash
sudo apt install python3 python3-venv
sudo snap install chromium
```

The u64deck installer itself does not invoke `sudo`:

```bash
tar -xzf u64deck-v1.9.0-linux-preview.7.tar.gz
cd u64deck
./install.sh
./u64deck.sh
```

`install.sh` is idempotent. It creates `.venv` in that extracted application
folder and installs only the Python packages from `requirements.txt` into it.
It also creates a per-user desktop entry and `~/.local/bin/u64deck` launcher.
Run `./u64deck.sh --linux-print-paths` to print the active persistent paths.

## Platform separation

- **Launcher:** Windows uses `start.bat`/the frozen EXE; Linux uses
  `u64deck.sh` and `linux/entry.py`.
- **Browser:** the Windows frozen build owns its Edge app window. Linux launches
  Chromium, Chrome or Edge with `--app`; if none is available it uses the
  system browser or prints the URL. Only a Linux app process launched by the
  current u64deck process is terminated on Exit.
- **Exit:** the shared RC48 `/api/app/exit` endpoint stops Uvicorn and reaches
  normal application cleanup. The Linux wrapper then terminates only its owned
  app-browser process group and removes its PID file.
- **Paths:** Windows stores persistent data beside `u64deck.exe`; Linux uses XDG
  per-user directories as documented below.

The Linux runtime verifies `linux/core-manifest.sha256`, generates an ephemeral
runtime facade under the XDG cache and runs the byte-identical RC48 shared modules
through it. The generated facade changes only release identity, XDG root paths
and Linux wording. The original `server.py`, discovery, indexing, SIDFlow,
device-control and static source files remain unchanged.

## Persistent files

### Windows

All persistent JSON and SQLite files are beside `u64deck.exe`:

- `config.json`
- `.u64deck-index.sqlite3`
- `.sidflow-similarity.sqlite`
- `user_items.json`
- `playlists.json`

### Linux

- Configuration: `${XDG_CONFIG_HOME:-~/.config}/u64deck/config.json`
- Indexes, SIDFlow, favourites, playlists and library data:
  `${XDG_DATA_HOME:-~/.local/share}/u64deck/`
- Logs and PID state: `${XDG_STATE_HOME:-~/.local/state}/u64deck/`
- Generated runtime, UI overlay and browser profile:
  `${XDG_CACHE_HOME:-~/.cache}/u64deck/`

Dropping Windows JSON/SQLite files beside `install.sh` or `u64deck.sh` does not
make Linux use them. Import them explicitly:

```bash
./import-existing-data.sh /path/to/windows-or-older-linux-u64deck
```

Use **Exit u64deck** before copying or importing SQLite databases. Never copy
`-wal` or `-shm` files. Favourites, playlists and SIDFlow data are generally
portable. Storage and SID indexes can contain absolute Windows paths; update
path settings or rebuild indexes against the Linux-mounted directories when
necessary.

## Upgrade and rollback

Extract each preview into a new folder, stop the previous process and run:

```bash
./update-linux.sh
```

The updater retains XDG state and repoints the per-user launchers. The previous
source folder is not deleted. To repoint to an earlier installed folder:

```bash
./update-linux.sh --rollback /path/to/previous/u64deck
```

## First-run notes

A new `config.json` has Auto F7 disabled by default. Legacy Retro Replay users
who deliberately want the physical-F7 guidance can enable **Auto F7 Fastload**
in the Screen tab or set `"boot_prekey": "F7"` in the XDG `config.json`.
Automatic injected F7 remains suppressed on the Legacy KERNAL-buffer path; the
setting enables the physical-F7 overlay/guidance.

The tested Wayland/Vulkan and Snap/AppArmor console warnings did not prevent
video, audio, fullscreen or recording. Do not force X11 unless diagnosing an
actual rendering problem.
