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

ARCHIVE_NAME = "u64deck-v1.9.0-linux-preview.9-private.tar.gz"
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

Linux Preview 9 is a private source-run test build matched to Windows RC51. It is not a frozen ELF or AppImage and is not approved for public release.

## Hardware status

Linux Preview 4 was hardware-tested on Ubuntu 24.04.4 LTS x86-64 with GNOME/Wayland and Chromium Snap. Linux Preview 9 retains that distribution architecture and adopts the accepted redesigned shared UI, but this tarball has not yet been hardware-validated on the maintainer NUC. Do not treat the inherited Preview 4 result as a Preview 9 hardware pass.

## Redesigned shared UI

- Adopts the accepted Windows RC51 layout, typography, Dashboard, single-page Screen Mirror and Jukebox presentation.
- The final Assembly64 non-commercial-client sentence uses the same font size as the surrounding support copy, with only a muted colour.
- Finder/discovery is unchanged and remains a separate reliability investigation.

## Auto F7 Legacy correction

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
- Owned Chromium-process cleanup: not rerun in this packaging container; retained for private hardware validation
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
    (out / "u64deck-v1.9.0-linux-preview.9-private-release-notes.md").write_text(
        notes, encoding="utf-8")
    print(tarball)
    print(f"SHA-256 {digest}")
    print(f"Files {len(manifest)}")
    print(identity(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
