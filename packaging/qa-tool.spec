# PyInstaller: сборка onedir для Windows и Linux. Каталог ffmpeg передаётся через QA_FFMPEG_BIN.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
ffmpeg_bin = Path(os.environ["QA_FFMPEG_BIN"])
exe = ".exe" if os.name == "nt" else ""

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=[(str(ffmpeg_bin / f"{tool}{exe}"), "ffmpeg") for tool in ("ffmpeg", "ffprobe")],
    datas=[
        (str(root / "docs" / "manual"), "manual"),
        (str(ffmpeg_bin.parent / "LICENSE.txt"), "ffmpeg"),  # LGPL: лицензия рядом с ffmpeg
        *collect_data_files("qa_tool"),
    ],
    hiddenimports=collect_submodules("qa_tool"),
    excludes=["tests", "pytest"],
)
pyz = PYZ(a.pure)
exe_ = EXE(pyz, a.scripts, [], exclude_binaries=True, name="qa-tool", console=False,
           icon=str(root / "packaging" / "icon.ico") if os.name == "nt" else None)
coll = COLLECT(exe_, a.binaries, a.datas, name="qa-tool")
