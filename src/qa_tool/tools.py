"""Внешние программы: ffmpeg и ffprobe — из сборки, иначе из PATH."""

import os
import shutil
import sys
from pathlib import Path


def tool_path(name: str) -> str | None:
    """Путь к ffmpeg или ffprobe: из собранной программы, из папки QA_TOOL_FFMPEG_DIR
    (разработка и тесты), иначе из PATH; None — нет."""
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg" / exe
    if getattr(sys, "frozen", False) and bundled.exists():
        return str(bundled)
    folder = os.environ.get("QA_TOOL_FFMPEG_DIR")
    if folder and (Path(folder) / exe).exists():
        return str(Path(folder) / exe)
    return shutil.which(name)


def bundled_folder(name: str) -> Path | None:
    """Папка из собранной программы; None — не сборка или папки в ней нет."""
    folder = Path(getattr(sys, "_MEIPASS", "")) / name
    return folder if getattr(sys, "frozen", False) and folder.is_dir() else None


def use_bundled_browser() -> None:
    """Указать Playwright на браузер из сборки: скачивания при запуске нет (S23)."""
    folder = bundled_folder("browser")
    if folder is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(folder)


def browser_env() -> dict[str, str] | None:
    """Окружение процесса браузера: библиотеки из сборки только для него, а не для всей программы.

    Returns:
        Окружение; None — наследовать обычное.
    """
    libs = bundled_folder("browser-libs")
    if libs is None:
        return None
    return {**os.environ, "LD_LIBRARY_PATH": str(libs)}
