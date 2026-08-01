"""Deterministic Linux Preview build identity."""
from __future__ import annotations

import hashlib
from pathlib import Path

PREVIEW_VERSION = "1.9.0"
PREVIEW_LABEL = "Linux Preview 5"
BASE_RELEASE = "Windows v1.9.0 RC45"
BASE_BUILD = "6feaede"


def _release_inputs(root: Path) -> list[Path]:
    names = [
        "install.sh",
        "u64deck.sh",
        "update-linux.sh",
        "uninstall-linux.sh",
        "import-existing-data.sh",
    ]
    paths = [root / name for name in names]
    paths.extend(sorted((root / "linux").glob("*.py"), key=lambda p: p.name.lower()))
    return [p for p in paths if p.is_file()]


def linux_build_id(root: Path | None = None) -> str:
    root = Path(root or Path(__file__).resolve().parents[1])
    digest = hashlib.sha1()
    for path in _release_inputs(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:7]


def identity(root: Path | None = None) -> str:
    return f"u64deck v{PREVIEW_VERSION} · {PREVIEW_LABEL} · build {linux_build_id(root)}"
