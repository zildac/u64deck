"""Release gate for the canonical README screenshot gallery."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
CANONICAL_IMAGES = (
    "docs/screen-tab.png",
    "docs/storage-search.png",
    "docs/jukebox.png",
    "docs/favourites.png",
    "docs/assembly64.png",
    "docs/settings.png",
    "docs/device-finder.png",
    "docs/wifi-ethernet-header.png",
    "docs/split-route.png",
    "docs/wifi-streaming-gated.png",
    "docs/mount-and-run.png",
    "docs/busy-loading.png",
    "docs/disk-swap.png",
)


def gallery_images() -> list[str]:
    text = README.read_text(encoding="utf-8")
    section = text.split("## Screenshots", 1)[1].split("## Quick start", 1)[0]
    return re.findall(r"!\[[^\]]+\]\((docs/[^)]+\.png)\)", section)


def check_gallery() -> list[str]:
    errors: list[str] = []
    images = gallery_images()
    if images != list(CANONICAL_IMAGES):
        errors.append(
            "README gallery does not match the canonical ordered list: "
            + ", ".join(CANONICAL_IMAGES)
        )
    missing = [image for image in CANONICAL_IMAGES if not (ROOT / image).is_file()]
    if missing:
        errors.append("missing gallery files: " + ", ".join(missing))
    return errors


if __name__ == "__main__":
    problems = check_gallery()
    if problems:
        for problem in problems:
            print(f"gallery gate: {problem}")
        raise SystemExit(1)
    print(f"gallery gate: {len(CANONICAL_IMAGES)} images present and ordered")
