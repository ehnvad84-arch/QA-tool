#!/usr/bin/env bash
# Библиотеки браузера для компьютера стенда: на чистой Ubuntu их нет, а интернета может не быть.
# Системные базовые библиотеки не берутся: копия glibc сломала бы запуск на другой версии Ubuntu.
#   packaging/linux/browser_libs.sh <папка браузера> <папка для библиотек>
set -euo pipefail
browser="$1"
target="$2"
mkdir -p "$target"
shell="$(find "$browser" -type f -name 'chrome-headless-shell' | head -1)"
[ -n "$shell" ] || { echo "браузер не найден в $browser" >&2; exit 1; }
missing="$(ldd "$shell" | awk '/not found/ {print $1}')"
[ -z "$missing" ] || { echo "на машине сборки нет библиотек браузера: $missing" >&2; exit 1; }
ldd "$shell" | awk '/=> \// {print $3}' | while read -r lib; do
  case "$(basename "$lib")" in
    libc.so*|libm.so*|libdl.so*|librt.so*|libpthread.so*|ld-linux*|libgcc_s.so*|libstdc++.so*) ;;
    *) cp -L "$lib" "$target/" ;;
  esac
done
