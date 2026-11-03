#!/usr/bin/env bash
# Сборка AppImage для Linux: PyInstaller (onedir) + ffmpeg LGPL + appimagetool.
#   packaging/linux/build.sh        → dist/QA-tool-<версия>-x86_64.AppImage
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD"

uv sync --group build --quiet
version="$(uv run python -c 'from qa_tool import __version__; print(__version__)')"
export QA_FFMPEG_BIN="$(uv run python packaging/fetch.py ffmpeg-linux)"
appimagetool="$(uv run python packaging/fetch.py appimagetool)"
runtime="$(uv run python packaging/fetch.py appimage-runtime)"

uv run python packaging/make_icon.py build/icon
cp build/icon/icon.ico packaging/icon.ico 2>/dev/null || true
uv run pyinstaller --noconfirm --clean --distpath build/pyinstaller --workpath build/work packaging/qa-tool.spec

appdir="build/QA-tool.AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr/lib"
cp -a build/pyinstaller/qa-tool "$appdir/usr/lib/qa-tool"
cp build/icon/icon.png "$appdir/qa-tool.png"
cat > "$appdir/qa-tool.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=QA-tool
Comment=Стенд проверки IP-камер
Exec=qa-tool
Icon=qa-tool
Categories=Utility;
Terminal=false
DESKTOP
cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/lib/qa-tool/qa-tool" "$@"
APPRUN
chmod +x "$appdir/AppRun"

mkdir -p dist
out="dist/QA-tool-${version}-x86_64.AppImage"
# без FUSE (контейнер, CI) appimagetool запускается распаковкой
ARCH=x86_64 "$appimagetool" --appimage-extract-and-run --no-appstream --runtime-file "$runtime" "$appdir" "$out" >/dev/null
echo "Готово: $root/$out"
