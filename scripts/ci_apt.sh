#!/usr/bin/env bash
# Системные библиотеки Qt на раннере Linux CI.
# 11.11.2026 зеркало раннеров azure.archive.ubuntu.com весь день не разрешалось по DNS, и сборки
# падали до первого теста. Основное зеркало Ubuntu и повторы скачивания убирают эту зависимость.
set -euo pipefail
for f in /etc/apt/apt-mirrors.txt /etc/apt/sources.list /etc/apt/sources.list.d/*; do
  [ -f "$f" ] && sudo sed -i 's/azure\.archive\.ubuntu\.com/archive.ubuntu.com/g' "$f"
done
sudo apt-get -o Acquire::Retries=5 update
sudo apt-get -o Acquire::Retries=5 install -y libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 fonts-dejavu-core
