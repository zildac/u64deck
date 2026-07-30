# PyInstaller spec for u64deck — builds a single self-contained executable.
#   pyinstaller u64deck.spec
# On Windows this produces dist/u64deck.exe (no Python install needed to run).

from pathlib import Path

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(SPECPATH)))
from release import BUILD_STAMP_NAME, source_build_id

source_root = Path(globals().get("SPECPATH", ".")).resolve()
stamp_dir = source_root / "build"
stamp_dir.mkdir(parents=True, exist_ok=True)
stamp_path = stamp_dir / BUILD_STAMP_NAME
stamp_value = source_build_id(source_root, source_root)
stamp_path.write_text(stamp_value + "\n", encoding="ascii", newline="\n")
print(f"u64deck packaging build stamp: {stamp_value}")

a = Analysis(
    ["server.py"],
    pathex=[str(source_root)],
    datas=[
        ("static", "static"),             # web UI bundled inside the exe
        (str(stamp_path), "."),            # exact source build identity
    ],
    hiddenimports=[
        # uvicorn loads these dynamically; PyInstaller can't see them
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
    ],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="u64deck",
    console=True,           # keep the console: shows the URL + any errors
    upx=False,
    icon="u64deck.ico",
)
