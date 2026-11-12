"""Пульт прогона, общий для всего окна: запуск, остановка, выпуск протоколов (S7, S13).

Прогон идёт в фоновом потоке, события планировщика приходят в поток окна через сигналы Qt.
Экран «Прогон» и блок прогона в колонке разделов только подписываются на эти сигналы,
поэтому запустить и остановить прогон можно из любого раздела.
"""

import threading
import time

from PySide6.QtCore import QObject, Signal

from qa_tool.core.config import load_thresholds
from qa_tool.core.models import ConfigError
from qa_tool.core.scheduler import Runner
from qa_tool.core.secrets import redact


class RunControl(QObject):
    """Один прогон за раз. `start()` и `stop()` вызываются из потока окна.

    Сигналы: `started` — прогон запущен; `event(Event)` — событие планировщика;
    `stopping` — остановка запрошена; `published(RunStore | None, str)` — прогон закончен,
    протоколы выпущены (или текст ошибки); `failed(str)` — прогон не запустился.
    """

    started = Signal()
    event = Signal(object)
    stopping = Signal()
    published = Signal(object, str)
    failed = Signal(str)
    _finished = Signal(object)
    _failed = Signal(str)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.runner: Runner | None = None
        self.last_store = None
        self.started_at = 0.0
        self.stop_requested = False
        self._finished.connect(self._publish)
        self._failed.connect(self._on_failed)

    @property
    def running(self) -> bool:
        """Идёт ли прогон (включая остановку и выпуск протоколов)."""
        return self.state.running

    def start(self) -> bool:
        """Запустить прогон стенда; False — не запущен (ошибка порогов, нет камер, уже идёт)."""
        state = self.state
        if state.running or not state.stand.cameras:
            return False
        try:
            thresholds = (
                load_thresholds(state.thresholds_path) if state.thresholds_path.exists() else {}
            )
        except ConfigError as e:
            self.failed.emit(str(e))
            return False
        self.runner = Runner(
            state.stand,
            state.scenario,
            thresholds,
            registry_ref=state.registry_ref,
            on_event=self.event.emit,
        )
        state.running = True
        self.stop_requested = False
        self.last_store = None  # иначе при сбое выпуска окно открыло бы протокол прошлого прогона
        self.started_at = time.monotonic()
        self.started.emit()
        runner = self.runner

        def work():
            try:
                store = runner.run()
            except ConfigError as e:
                self._failed.emit(str(e))
                return
            except Exception as e:  # иначе state.running навсегда True и «Начать» недоступна
                self._failed.emit(f"ошибка стенда: {type(e).__name__}: {redact(str(e))}")
                return
            store.close()  # база SQLite привязана к потоку: окно откроет её заново у себя
            self._finished.emit(store.run_dir)

        threading.Thread(target=work, daemon=True).start()
        return True

    def stop(self) -> None:
        """Остановить прогон: начатые проверки доработают, протоколы выйдут по сделанному."""
        if self.runner and not self.stop_requested:
            self.stop_requested = True
            self.runner.stop()
            self.stopping.emit()

    def elapsed_s(self) -> float:
        """Сколько секунд идёт текущий прогон."""
        return time.monotonic() - self.started_at if self.state.running else 0.0

    def _publish(self, run_dir) -> None:
        from qa_tool.core.store import RunStore
        from qa_tool.report.publish import publish

        try:
            store = RunStore(run_dir)
            publish(store)
            self.last_store = store
            error = ""
        except Exception as e:  # протокол не вышел — сообщаем, но окно не зависает в «идёт»
            store, error = None, f"протоколы не выпущены: {redact(str(e))}"
        self._done()
        self.published.emit(store, error)

    def _on_failed(self, error: str) -> None:
        self._done()
        self.failed.emit(error)

    def _done(self) -> None:
        self.state.running = False
        self.runner = None
