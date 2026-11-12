import os
import sys
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

# ffmpeg и ffprobe закреплённой сборки из кэша packaging/fetch.py — тем же, что в установщиках
for _bin in sorted((Path.home() / ".cache" / "qa-tool-build").glob("ffmpeg-*/*/bin")):
    os.environ.setdefault("QA_TOOL_FFMPEG_DIR", str(_bin))

# выборка потока на всём прогоне: иначе каждый тест с mock-камерой шёл бы на 30 с дольше
os.environ.setdefault("QA_TOOL_STREAM_SAMPLE_S", "1")

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # тесты окна и PDF без экрана


class MemoryKeyring(KeyringBackend):
    """Хранилище паролей в памяти: тесты не трогают системное."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def user_settings(tmp_path):
    """Настройки пользователя (тема окна) — во временной папке, а не в профиле."""
    from PySide6.QtCore import QSettings

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    for slot in range(1, 9):
        monkeypatch.delenv(f"QA_TOOL_PASSWORD_{slot}", raising=False)
    yield backend
    keyring.set_keyring(previous)
