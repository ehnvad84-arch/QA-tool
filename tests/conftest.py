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


_real_stderr_fd: int | None = None


def pytest_configure(config):
    """Копия настоящего stderr для сторожа: на время теста pytest подменяет fd 2 своим файлом,
    и после `os._exit` перехваченное не печатается. В pytest_configure перехват ещё снят."""
    global _real_stderr_fd
    if os.environ.get("QA_TOOL_TEST_WATCHDOG_S"):
        _real_stderr_fd = os.dup(sys.__stderr__.fileno())


def _dump_and_exit(nodeid: str, limit_s: float) -> None:
    """Напечатать зависший тест и стеки всех потоков и greenlet, затем снять прогон."""
    import faulthandler
    import gc
    import traceback

    fd = _real_stderr_fd if _real_stderr_fd is not None else sys.__stderr__.fileno()
    out = os.fdopen(fd, "w", closefd=False)
    print(f"\n!!! тест висит дольше {limit_s:.0f} с: {nodeid}", file=out, flush=True)
    faulthandler.dump_traceback(file=out, all_threads=True)
    try:
        from greenlet import greenlet
    except ImportError:
        greenlet = None
    if greenlet is not None:
        for item in gc.get_objects():
            if isinstance(item, greenlet) and item.gr_frame is not None:
                print("--- greenlet:", file=out)
                traceback.print_stack(item.gr_frame, file=out)
    out.flush()
    os._exit(3)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Сторож CI: тест дольше QA_TOOL_TEST_WATCHDOG_S снимает прогон и называет себя.

    faulthandler показывает только поток, а Playwright ждёт ответа драйвера в своём greenlet,
    и стек самого теста не виден; без сторожа зависание съедало час задания CI.
    """
    limit = float(os.environ.get("QA_TOOL_TEST_WATCHDOG_S", "0"))
    if limit <= 0:
        return (yield)
    import threading

    timer = threading.Timer(limit, _dump_and_exit, (item.nodeid, limit))
    timer.daemon = True
    timer.start()
    try:
        return (yield)
    finally:
        timer.cancel()


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
