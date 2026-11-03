"""Экран «Прогон»: «Начать» / «Остановить», 8 живых плиток по местам стенда."""

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from qa_tool.core.checks import Status
from qa_tool.core.config import load_thresholds
from qa_tool.core.models import MAX_SLOTS, ConfigError
from qa_tool.core.scheduler import Event, Runner
from qa_tool.core.secrets import redact
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen


class _RunSignals(QObject):
    event = Signal(object)
    finished = Signal(object)
    failed = Signal(str)


class Tile(QGroupBox):
    def __init__(self, slot: int):
        super().__init__(f"Место {slot}")
        self.slot = slot
        layout = QVBoxLayout(self)
        self.camera = QLabel("свободно")
        self.current = QLabel("")
        self.current.setWordWrap(True)
        self.counts = QLabel("")
        self.progress = helped(
            QProgressBar(), f"Сколько проверок места {slot} уже выполнено.", "run-tiles"
        )
        for w in (self.camera, self.current, self.counts, self.progress):
            layout.addWidget(w)
        self.reset(None, 0)

    def reset(self, camera, total: int) -> None:
        self.tally = {s.value: 0 for s in Status}
        self.camera.setText(
            f"{camera.address}:{camera.port} · {camera.vendor}" if camera else "свободно"
        )
        self.current.setText("ожидание" if camera else "")
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.progress.setVisible(camera is not None)
        self._show_counts()

    def on_result(self, status: str) -> None:
        self.tally[status] = self.tally.get(status, 0) + 1
        self.progress.setValue(self.progress.value() + 1)
        self._show_counts()

    def _show_counts(self) -> None:
        t = self.tally
        self.counts.setText(
            f"прошла {t['прошла']} · не прошла {t['не прошла']} · "
            f"пропущена {t['пропущена']} · ошибка стенда {t['ошибка стенда']}"
        )


class RunScreen(Screen):
    title = "Прогон"
    help_id = "run"
    hint = "Запуск прогона и ход проверок по каждой камере"
    intro = "Запустите прогон и следите за проверками по каждому месту."

    def build(self) -> None:
        self.runner: Runner | None = None
        self.last_store = None
        self.signals = _RunSignals()
        self.signals.event.connect(self._on_event)
        self.signals.finished.connect(self._on_finished)
        self.signals.failed.connect(self._on_failed)

        row = QHBoxLayout()
        self.start_button = helped(
            QPushButton("Начать прогон"),
            "Запустить проверки сценария на всех камерах стенда одновременно.",
            "run-start",
        )
        self.stop_button = helped(
            QPushButton("Остановить"),
            "Остановить прогон. Начатые проверки "
            "доработают, остальные будут «пропущены», протоколы выпустятся по "
            "сделанному.",
            "run-stop",
        )
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.phase = QLabel("Прогон не запущен.")
        self.layout_.addWidget(self.phase)

        grid = QGridLayout()
        self.tiles = {slot: Tile(slot) for slot in range(1, MAX_SLOTS + 1)}
        for slot, tile in self.tiles.items():
            grid.addWidget(tile, (slot - 1) // 4, (slot - 1) % 4)
        self.layout_.addLayout(grid)
        self.refresh()

    def refresh(self) -> None:
        if self.state.running:
            return
        registry = self.state.registry()
        for slot, tile in self.tiles.items():
            cam = self.state.stand.camera_at(slot)
            total = (
                len(registry.plan_for(cam, self.state.scenario, self.state.stand.place))
                if cam
                else 0
            )
            tile.reset(cam, total)
        self.start_button.setEnabled(bool(self.state.stand.cameras))

    def start(self) -> None:
        state = self.state
        try:
            thresholds = (
                load_thresholds(state.thresholds_path) if state.thresholds_path.exists() else {}
            )
        except ConfigError as e:
            QMessageBox.warning(self, "Прогон", str(e))
            return
        self.refresh()
        self.runner = Runner(
            state.stand,
            state.scenario,
            thresholds,
            registry_ref=state.registry_ref,
            on_event=self.signals.event.emit,
        )
        state.running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.phase.setText("Прогон запускается…")
        self.notify()

        def work():
            try:
                store = self.runner.run()
            except ConfigError as e:
                self.signals.failed.emit(str(e))
                return
            except Exception as e:  # иначе state.running навсегда True и «Начать» недоступна
                self.signals.failed.emit(f"ошибка стенда: {type(e).__name__}: {redact(str(e))}")
                return
            store.close()  # база SQLite привязана к потоку: окно откроет её заново у себя
            self.signals.finished.emit(store.run_dir)

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        if self.runner:
            self.runner.stop()
            self.stop_button.setEnabled(False)
            self.phase.setText("Останавливаем: ждём окончания начатых проверок…")

    def _on_event(self, event: Event) -> None:
        tile = self.tiles.get(event.slot)
        if event.kind == "phase":
            self.phase.setText(f"Фаза: {event.title}")
        elif event.kind == "started" and tile:
            tile.current.setText(f"идёт: {event.check_id} — {event.title}")
        elif event.kind == "result" and tile:
            tile.on_result(event.status)
            tile.current.setText(f"{event.check_id}: {event.status}")
        elif event.kind == "lost" and tile:
            tile.current.setText(event.detail)

    def _on_finished(self, run_dir) -> None:
        from qa_tool.core.store import RunStore
        from qa_tool.report.publish import publish

        self.phase.setText("Выпускаем протоколы…")
        try:
            store = RunStore(run_dir)
            publish(store)
            self.last_store = store
            state = store.run_info()["state"]
            self.phase.setText(f"Прогон {store.run_id} {state}. Протоколы — на экране «Протоколы».")
        except Exception as e:  # протокол не вышел — сообщаем, но окно не зависает в «идёт»
            self.phase.setText(f"Прогон закончен, но протоколы не выпущены: {e}")
            QMessageBox.warning(self, "Прогон", f"Протоколы не выпущены: {e}")
        finally:
            self._done()

    def _on_failed(self, error: str) -> None:
        self.phase.setText(f"Прогон не запущен: {error}")
        QMessageBox.warning(self, "Прогон", error)
        self._done()

    def _done(self) -> None:
        self.state.running = False
        self.runner = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.notify()
