"""Скачать закреплённые внешние инструменты сборки и проверить их SHA-256.

    python packaging/fetch.py ffmpeg-linux|ffmpeg-windows|appimagetool|appimage-runtime

Кэш — QA_BUILD_CACHE или ~/.cache/qa-tool-build. Печатает путь к каталогу bin или файлу.
Выпуски BtbN autobuild со временем удаляются — тогда обновить PINS (тег и хэш из checksums.sha256).
"""

import hashlib
import os
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-11-20-12-40/"
PINS = {
    # ffmpeg 8.1.3, вариант LGPL (статический), — хэши из checksums.sha256 выпуска
    "ffmpeg-linux": (
        _BTBN + "ffmpeg-n8.1.3-39-g660b5aa7db-linux64-lgpl-8.1.tar.xz",
        "689ef2543987e4c67ebe2dfddb86e4d0b1469e965483df0aa2916bbe6b218752",
    ),
    "ffmpeg-windows": (
        _BTBN + "ffmpeg-n8.1.3-39-g660b5aa7db-win64-lgpl-8.1.zip",
        "a16d63673d8b28ec0b4fb7311677fd8f6fa9f221f40bf00b48c01ce8e9122be8",
    ),
    # appimagetool 1.9.1 — у выпуска нет опубликованного хэша, закреплён хэш первой загрузки
    "appimagetool": (
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
    ),
    # среда запуска AppImage, выпуск 20251108 (без неё appimagetool качает «continuous»)
    "appimage-runtime": (
        "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
        "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d",
    ),
}


def cache_dir() -> Path:
    return Path(os.environ.get("QA_BUILD_CACHE", Path.home() / ".cache" / "qa-tool-build"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(name: str) -> Path:
    url, expected = PINS[name]
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / url.rsplit("/", 1)[1]
    if not archive.exists() or sha256(archive) != expected:
        tmp = archive.with_suffix(archive.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        actual = sha256(tmp)
        if actual != expected:
            tmp.unlink()
            raise SystemExit(f"{name}: SHA-256 не совпал ({actual}), сборка остановлена")
        tmp.replace(archive)
    if archive.suffix not in (".xz", ".zip"):
        archive.chmod(0o755)
        return archive
    target = root / (name + "-" + expected[:12])
    if not target.exists():
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
        else:
            with tarfile.open(archive) as t:
                t.extractall(target, filter="data")
    return next(target.glob("*/bin"))


if __name__ == "__main__":
    print(fetch(sys.argv[1]))
