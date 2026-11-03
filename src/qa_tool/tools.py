"""Внешние программы: ffmpeg и ffprobe — из сборки, иначе из PATH."""

import os
import shutil
import sys
from pathlib import Path


def tool_path(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg" / exe
    if getattr(sys, "frozen", False) and bundled.exists():
        return str(bundled)
    return shutil.which(name)
