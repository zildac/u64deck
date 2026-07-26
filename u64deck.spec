# PyInstaller spec for u64deck — builds a single self-contained executable.
#   pyinstaller u64deck.spec
# On Windows this produces dist/u64deck.exe (no Python install needed to run).

import sys

a = Analysis(
    ["server.py"],
    pathex=["."],
    datas=[("static", "static")],          # web UI bundled inside the exe
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
