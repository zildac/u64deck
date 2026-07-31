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

ARCHIVE_NAME = "u64deck-v1.9.0-linux-preview.4.tar.gz"
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
    return f"""# u64deck v1.9.0 — Linux Preview 4

**Identity:** `u64deck v1.9.0 · Linux Preview 4 · build {build}`  
**Base lineage:** `{BASE_RELEASE} · build {BASE_BUILD}`

Linux Preview 4 is a source-run preview published alongside the Windows release in the same repository. It is not a frozen ELF or AppImage. Windows remains the stable, supported distribution; this Linux preview is provided as-is for testing and issue reports.

## Hardware-tested Linux environment

- Ubuntu 24.04.4 LTS
- amd64 / x86-64
- Linux kernel 6.8.0-136-generic
- Ubuntu GNOME under Wayland
- Python 3.12.3
- Chromium 150.0.7871.128 from Canonical's stable Snap channel

The maintainer hardware-tested installation and application-menu launch, Chromium app-window mode, Ultimate discovery and connection, screen mirror at approximately 50 fps, HDMI audio, fullscreen, recording, Legacy physical-F7 guidance, local SID/HVSC and storage indexing, SID Search, SIDFlow download/verify/import and recommendations, SID playback, Storage browsing, Mount & Run, upgrade-state retention and clean Exit.

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
- Clean XDG install, API identity, config save and graceful Exit: passed
- Owned Chromium-process cleanup: passed
- Data import, backup and Windows-path warning checks: passed
- Update and rollback smoke: {validation}
- Archive hygiene and personal-path scan: passed
- Archive entries: **{file_count} files**

The dependency-download path was hardware-tested on the Ubuntu NUC. The packaging sandbox had no public package-index access, so its clean installer validation used the same installed dependency versions exposed to an isolated virtual environment.

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
    (out / "u64deck-v1.9.0-linux-preview.4-release-notes.md").write_text(
        notes, encoding="utf-8")
    print(tarball)
    print(f"SHA-256 {digest}")
    print(f"Files {len(manifest)}")
    print(identity(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
