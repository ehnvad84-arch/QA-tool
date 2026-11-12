"""Временные папки проверок: имена и уборка после снятого процесса.

Браузер веб-обхода (`qa-web-`), захват кадров картинки (`qa-image-`) и выборки кодеков
(`qa-codec-`) живут в процессе камеры. Если планировщик снял этот процесс, папка остаётся: у
браузера в ней куки сессии, у картинки и кодеков — выборки видео в десятки мегабайт. Поэтому
при старте прогона такие папки удаляются.
"""

import shutil
import tempfile
import time
from pathlib import Path

TEMP_PREFIX = "qa-web-"
IMAGE_PREFIX = "qa-image-"
CODEC_PREFIX = "qa-codec-"
PREFIXES = (TEMP_PREFIX, IMAGE_PREFIX, CODEC_PREFIX)
# Папка моложе часа может принадлежать идущему прогону другой копии программы
STALE_AFTER_S = 3600.0


def remove_stale_temp_files(folder: Path | None = None) -> int:
    """Удалить папки браузера, кадров и выборок кодеков, оставшиеся от снятых процессов.

    Args:
        folder: где искать; по умолчанию системная временная папка.

    Returns:
        Сколько папок удалено.
    """
    root = folder or Path(tempfile.gettempdir())
    removed = 0
    for path in [p for prefix in PREFIXES for p in root.glob(f"{prefix}*")]:
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > STALE_AFTER_S:
                shutil.rmtree(path, ignore_errors=True)
                removed += not path.exists()
        except OSError:
            continue  # папку убрал кто-то другой: цель достигнута
    return removed
