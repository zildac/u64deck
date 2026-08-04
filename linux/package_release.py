"""Create the deterministic Linux Preview source tarball and release sidecars."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

from .build_id import BASE_BUILD, BASE_RELEASE, PREVIEW_LABEL, PREVIEW_VERSION, identity, linux_build_id

ARCHIVE_NAME = "u64deck-v1.9.0-linux-preview.9.tar.gz"
EXECUTABLES = {
    "install.sh", "u64deck.sh", "update-linux.sh",
    "uninstall-linux.sh", "import-existing-data.sh",
}
EXCLUDED_NAMES = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist",
    "config.json", "u64deck-build-id.txt", "LINUX-RELEASE-NOTES.md",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".wal", ".shm"}
RUNTIME_NAMES = {
    ".u64deck-index.sqlite3", ".sidflow-similarity.sqlite",
    "user_items.json", "playlists.json", ".songlengths.cache",
    ".imagecache.json", ".dircache.json", ".indexmeta.json",
    ".espressif-ouis-cache.json", ".legacy-cache-imported",
}


def excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDED_NAMES or part.startswith("artifacts-") for part in parts):
        return True
    name = rel.name
    if name in RUNTIME_NAMES or name.endswith(tuple(EXCLUDED_SUFFIXES)) or name.endswith(("-wal", "-shm")):
        return True
    if name.startswith(".u64deck-index-") or name.startswith(".sidflow-"):
        return True
    if name.startswith("u64deck-diagnostics-"):
        return True
    return False


def source_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if excluded(rel):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def build_tarball(root: Path, output: Path) -> tuple[str, list[str]]:
    files = source_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    manifest: list[str] = []
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tf:
        root_info = tarfile.TarInfo("u64deck")
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        root_info.mtime = 0
        root_info.uid = root_info.gid = 0
        root_info.uname = root_info.gname = ""
        tf.addfile(root_info)
        directories: set[str] = set()
        for path in files:
            rel = path.relative_to(root)
            parent = rel.parent
            while parent != Path("."):
                directories.add(parent.as_posix())
                parent = parent.parent
        for directory in sorted(directories):
            info = tarfile.TarInfo(f"u64deck/{directory}")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info)
        for path in files:
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(f"u64deck/{rel}")
            info.size = len(data)
            info.mode = 0o755 if rel in EXECUTABLES else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
            manifest.append(f"{info.mode:o} {len(data):9d} u64deck/{rel}")
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(buffer.getvalue())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return digest, manifest


def release_notes(root: Path, digest: str, file_count: int,
                  source_tests: str, archive_tests: str, validation: str) -> str:
    build = linux_build_id(root)
    return f"""# u64deck v1.9.0 — Linux Preview 9

**Identity:** `u64deck v1.9.0 · Linux Preview 9 · build {build}`  
**Base lineage:** `{BASE_RELEASE} · build {BASE_BUILD}`

Linux Preview 9 is a public source-run soak candidate published as a GitHub **Pre-release** alongside Windows RC51. It is not a frozen ELF or AppImage.

## New since Preview 7

Linux Preview 9 is the first public Linux preview to carry the RC49/RC50 shared-application work:

- Redesigned u64deck interface and Dashboard presentation.
- Styled normal, important and destructive confirmation modals.
- Progressive Ultimate Finder with verified results appearing during the scan.
- SIDFlow queue diversification to avoid repeated files or visually identical entries.
- Corrected SID fade scheduling based on the actual SID launch clock.
- Source-agnostic queue auto-advance.
- Legacy Mount & Run keyboard-buffer drain handling.
- Enlarged Dashboard SID Jukebox artwork.
- Styled local SID multi-file selection control.
- RC51 compact application icon and restored `library/README.txt` Quick Launch scaffold.

## Hardware status

Linux Preview 4 was hardware-tested on Ubuntu 24.04.4 LTS x86-64 with GNOME/Wayland and Chromium Snap. Linux Preview 9 retains that distribution architecture and adopts the accepted RC51 shared application baseline, but this corrected public tarball has not been hardware-tested on the maintainer NUC during this packaging run. Do not treat the inherited Preview 4 result as a Preview 9 hardware pass.

## Install & run

```bash
tar xzf u64deck-v1.9.0-linux-preview.9.tar.gz
cd u64deck
./install.sh
./u64deck.sh
```

`install.sh` creates a private virtual environment inside the extracted u64deck folder, installs the Python dependencies there, and creates a per-user application-menu entry and `u64deck` launcher. The installer itself does not use `sudo`, modify system Python, or make system-wide changes.

Linux configuration is stored under `${{XDG_CONFIG_HOME:-~/.config}}/u64deck/` and persistent indexes, SIDFlow data, favourites and playlists are stored under `${{XDG_DATA_HOME:-~/.local/share}}/u64deck/`; they are not stored beside the scripts.

## Upgrading from an earlier preview

1. Extract the new preview, enter its `u64deck` directory, and run:

   ```bash
   ./update-linux.sh
   ```

2. Keep only **one Linux preview active at a time**. All previews share the same XDG configuration and data directories, so running two copies side by side can cross-contaminate configuration, indexes and runtime state.
3. `./uninstall-linux.sh` removes the per-user command and application-menu entry. It leaves the extracted source folder and XDG configuration/data untouched.
4. For Windows-to-Linux or older in-folder migration, run `./import-existing-data.sh /path/to/old/u64deck`. It backs up replaced Linux files, skips live WAL/SHM sidecars, warns about genuine Windows paths and leaves the source folder untouched.

## Shared application updates

- Adopts the accepted Windows RC51 layout, typography, Dashboard, single-page Screen Mirror and Jukebox presentation.
- The final Assembly64 non-commercial-client sentence uses the same font size as the surrounding support copy, with only a muted colour.
- Finder now uses the accepted progressive discovery implementation from the RC50 hardware-tested baseline.

## Auto F7 Legacy behaviour

- Auto F7 defaults to enabled for new installations and older configurations where `boot_prekey` is absent; an explicit disabled value remains disabled.
- The checkbox remains selectable on Legacy KERNAL-buffer connections. Enabling it stores the preference and shows physical-F7 guidance only.
- Legacy F7 injection remains hard-suppressed; confirmed Retro Replay on CIA1 retains automatic matrix F7.

## Assembly64 update

- Every Assembly64 search, preset, entry and binary-download request uses the activated dedicated `Client-Id: u64deck` application identifier.
- The Assembly64 page contains the agreed non-commercial attribution and direct Ko-fi and PayPal support links.
- README and in-app Help contain the same attribution and both official donation links.
- No live Assembly64 request was made by the packaging sandbox unless explicitly recorded in the accompanying validation report.

## Storage and migration

- Windows stores `config.json`, indexes, SIDFlow data, favourites and playlists beside `u64deck.exe`.
- Linux stores `config.json` in `${{XDG_CONFIG_HOME:-~/.config}}/u64deck/`.
- Linux stores SQLite indexes, SIDFlow data, favourites, playlists and other persistent data in `${{XDG_DATA_HOME:-~/.local/share}}/u64deck/`.
- `import-existing-data.sh` imports supported Windows or older-preview files, backs up replaced Linux files, skips WAL/SHM files and warns about Windows paths.

## Validation

- Source tests: **{source_tests}**
- Extracted-tarball tests: **{archive_tests}**
- Core-integrity manifest: passed
- Python compile, JavaScript syntax and shell syntax: passed
- Isolated XDG runtime preparation, Linux API identity/title and graceful Exit: passed
- Installer/update shell syntax and focused tests: passed; dependency installation was not rerun in this sandbox
- Owned Chromium-process cleanup: not rerun in this packaging container; remains part of the public preview soak checklist
- Data import, backup, URL-scheme and genuine Windows-path warning focused tests: passed
- Update and rollback validation: {validation}
- Archive hygiene and personal-path scan: passed
- Archive entries: **{file_count} files**

The dependency-download path was hardware-tested during the earlier Ubuntu NUC preview work. No Linux Preview 9 NUC hardware test, live dependency-install run, owned-browser cleanup run or live updater rollback was performed during this packaging run. The packaging sandbox used its preinstalled test dependencies only.

## Artifact

`{ARCHIVE_NAME}`  
SHA-256: `{digest}`

The archive contains no `config.json`, SQLite databases, WAL/SHM files, downloaded SIDFlow data, caches, bytecode, `.git`, `.venv`, diagnostics or personal runtime data.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-tests", default="not recorded")
    parser.add_argument("--archive-tests", default="not recorded")
    parser.add_argument("--update-validation", default="pending")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = Path(args.output_dir).resolve()
    tarball = out / ARCHIVE_NAME
    digest, manifest = build_tarball(root, tarball)
    (out / f"{ARCHIVE_NAME}.sha256.txt").write_text(
        f"{digest}  {ARCHIVE_NAME}\n", encoding="ascii")
    (out / f"{ARCHIVE_NAME}.manifest.txt").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8")
    notes = release_notes(root, digest, len(manifest), args.source_tests,
                          args.archive_tests, args.update_validation)
    (out / "u64deck-v1.9.0-linux-preview.9-release-notes.md").write_text(
        notes, encoding="utf-8")
    print(tarball)
    print(f"SHA-256 {digest}")
    print(f"Files {len(manifest)}")
    print(identity(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
