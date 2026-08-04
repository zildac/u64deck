# PyInstaller spec for u64deck — builds a single self-contained executable.
#   pyinstaller u64deck.spec
# On Windows this produces dist/u64deck.exe (no Python install needed to run).

import sys
from pathlib import Path
sys.path.insert(0, str(Path(globals().get("SPECPATH", ".")).resolve()))
from release import BUILD_STAMP_NAME, RELEASE_LABEL, VERSION, source_build_id

source_root = Path(globals().get("SPECPATH", ".")).resolve()
stamp_dir = source_root / "build"
stamp_dir.mkdir(parents=True, exist_ok=True)
stamp_path = stamp_dir / BUILD_STAMP_NAME
stamp_value = source_build_id(source_root, source_root)
stamp_path.write_text(stamp_value + "\n", encoding="ascii", newline="\n")
print(f"u64deck packaging build stamp: {stamp_value}")

version_parts = [int(part) for part in VERSION.split(".")]
while len(version_parts) < 4:
    version_parts.append(0)
version_tuple = tuple(version_parts[:4])
version_info_path = stamp_dir / "u64deck-version-info.txt"
version_info_text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple!r},
    prodvers={version_tuple!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'u64deck community project'),
         StringStruct('FileDescription', 'u64deck - Ultimate 64 control deck'),
         StringStruct('FileVersion', '{VERSION}.0'),
         StringStruct('InternalName', 'u64deck'),
         StringStruct('OriginalFilename', 'u64deck.exe'),
         StringStruct('ProductName', 'u64deck'),
         StringStruct('ProductVersion', '{VERSION}.0'),
         StringStruct('ReleaseLabel', '{RELEASE_LABEL}'),
         StringStruct('BuildId', '{stamp_value}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
version_info_path.write_text(version_info_text, encoding="utf-8", newline="\n")

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
    version=str(version_info_path),
)
