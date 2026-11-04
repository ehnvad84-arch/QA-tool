"""Главное окно варианта D: колонка разделов с блоком прогона слева, экран справа."""

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from qa_tool import __version__
from qa_tool.core.models import MAX_SLOTS
from qa_tool.gui.help import help_id_of, helped
from qa_tool.gui.help_window import HelpWindow
from qa_tool.gui.run_control import RunControl
from qa_tool.gui.screens import SCREENS
from qa_tool.gui.state import AppState


def format_elapsed(seconds: float) -> str:
    """Длительность прогона для блока прогона: ЧЧ:ММ:СС."""
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def stop_question(parent: QWidget, closing: bool) -> QMessageBox:
    """Вопрос перед остановкой прогона (S13).

    Выбран ответ «Продолжить прогон», он же срабатывает на Esc: случайный Enter или
    закрытие окошка прогон не прерывают.

    Args:
        parent: окно, над которым показать вопрос.
        closing: вопрос задан при закрытии окна, а не по кнопке «Остановить».
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Идёт прогон")
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText("Идёт прогон. Остановить его?")
    tail = "окно закроется после этого." if closing else "их можно будет открыть в «Протоколах»."
    box.setInformativeText(
        f"Начатые проверки доработают, остальные будут «пропущены». Протоколы выйдут по "
        f"сделанному, {tail}"
    )
    box.addButton("Остановить и закрыть" if closing else "Остановить", box.ButtonRole.AcceptRole)
    keep = box.addButton("Продолжить прогон", box.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    return box


def ask_stop(parent: QWidget, closing: bool) -> bool:
    """Спросить оператора; True — остановить прогон."""
    box = stop_question(parent, closing)
    box.exec()
    return box.clickedButton() is not box.defaultButton()


class RunBlock(QFrame):
    """Блок прогона внизу колонки разделов (S7): состояние, время, фаза и кнопки.

    Виден на всех экранах; кнопки только зовут общий `RunControl`.
    """

    def __init__(self, control: RunControl, open_protocols, request_stop, parent=None):
        super().__init__(parent)
        self.control = control
        self.setProperty("role", "run-block")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        self.status = helped(
            QLabel("Прогон не запущен"),
            "Состояние прогона: сколько он идёт и какая фаза сейчас.",
            "run-block",
        )
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.phase = ""
        self.start_button = helped(
            QPushButton("Начать прогон"),
            "Запустить проверки сценария на всех камерах стенда одновременно.",
            "run-start",
        )
        self.start_button.setProperty("role", "primary")
        self.stop_button = helped(
            QPushButton("Остановить"),
            "Остановить прогон. Начатые проверки доработают, остальные будут «пропущены», "
            "протоколы выпустятся по сделанному.",
            "run-stop",
        )
        self.stop_button.setProperty("role", "link")
        self.protocols_button = helped(
            QPushButton("Открыть протоколы"),
            "Перейти к протоколам последнего прогона.",
            "protocols",
        )
        self.protocols_button.setProperty("role", "link")
        for w in (self.status, self.start_button, self.stop_button, self.protocols_button):
            layout.addWidget(w)
        self.start_button.clicked.connect(control.start)
        self.stop_button.clicked.connect(request_stop)
        self.protocols_button.clicked.connect(open_protocols)
        control.started.connect(self._started)
        control.event.connect(self._on_event)
        control.stopping.connect(self.update_view)
        control.published.connect(self._published)
        control.failed.connect(self._failed)
        self.result = ""
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_view)
        self.update_view()

    def _started(self) -> None:
        self.phase, self.result = "", ""
        self.timer.start()
        self.update_view()

    def _on_event(self, event) -> None:
        if event.kind == "phase":
            self.phase = event.title.lower()
            self.update_view()

    def _published(self, store, error: str) -> None:
        self.timer.stop()
        if store is not None:
            state = store.run_info()["state"]
            self.result = f"Прогон {store.run_id} {state}." + (f"\n{error}" if error else "")
        else:
            self.result = f"Прогон закончен, но {error}"
        self.update_view()

    def _failed(self, error: str) -> None:
        self.timer.stop()
        self.result = f"Прогон не выполнен: {error}"
        self.update_view()

    def update_view(self) -> None:
        """Показать состояние прогона и нужные кнопки."""
        c = self.control
        running = c.running
        if running and c.stop_requested:
            text = "Останавливаем: ждём начатые проверки и выпускаем протоколы…"
        elif running:
            text = f"Прогон идёт\n{format_elapsed(c.elapsed_s())}"
            if self.phase:
                text += f" · {self.phase}"
        else:
            text = self.result or "Прогон не запущен"
        self.status.setText(text)
        self.start_button.setVisible(not running)
        self.start_button.setEnabled(bool(c.state.stand.cameras))
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(running and not c.stop_requested)
        self.protocols_button.setVisible(not running and c.last_store is not None)


class MainWindow(QMainWindow):
    def __init__(self, state: AppState | None = None):
        super().__init__()
        self.state = state or AppState()
        self.setWindowTitle(f"QA-tool {__version__} — стенд проверки IP-камер")
        self.resize(1280, 800)
        self.setMinimumSize(1280, 800)
        self.run_control = RunControl(self.state, self)
        self.ask_stop = ask_stop  # тесты подставляют ответ оператора
        self.close_after_run = False

        self.menu = helped(
            QListWidget(), "Разделы программы. Выберите раздел, чтобы открыть его.", "index"
        )
        self.menu.setProperty("role", "nav-list")
        self.pages = QStackedWidget()
        self.screens = []
        for screen_cls in SCREENS:
            screen = screen_cls(self.state)
            self.screens.append(screen)
            self.pages.addWidget(screen)
            item = QListWidgetItem(screen.title)
            item.setToolTip(screen.hint)
            # высота строки задана явно: размеры пунктов Qt считает до применения стилей,
            # и в первой отрисовке пункты меню наезжали друг на друга
            item.setSizeHint(QSize(0, 40))
            self.menu.addItem(item)
        self.menu.currentRowChanged.connect(self.show_screen)

        nav = QWidget()
        nav.setProperty("role", "nav")
        nav.setFixedWidth(200)
        column = QVBoxLayout(nav)
        column.setContentsMargins(12, 20, 12, 20)
        brand = QLabel("QA-tool")
        brand.setProperty("role", "brand")
        column.addWidget(brand)
        column.addWidget(self.menu, 1)
        self.run_block = RunBlock(self.run_control, self.open_protocols, self.request_stop)
        column.addWidget(self.run_block)

        central = QWidget()
        central.setProperty("role", "page")
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(nav)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        for screen in self.screens:
            screen.bind_run(self.run_control)
        self.run_control.started.connect(self.update_status)
        self.run_control.published.connect(lambda *_: self.update_status())
        self.run_control.failed.connect(lambda *_: self.update_status())
        self.run_control.published.connect(lambda *_: self._close_if_requested())
        self.run_control.failed.connect(lambda *_: self._close_if_requested())

        self.status = helped(
            QLabel(), "Состояние стенда: место, сколько камер на местах, идёт ли прогон.", "index"
        )
        self.statusBar().addPermanentWidget(self.status, 1)
        self.help_window: HelpWindow | None = None
        f1 = QShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents), self, self.show_help)
        f1.setContext(Qt.ShortcutContext.ApplicationShortcut)  # F1 и в окнах-диалогах
        self.menu.setCurrentRow(1)  # начинаем с камер
        self.update_status()

    def help_target(self):
        """Элемент под мышью, иначе элемент в фокусе (кроме меню), иначе текущий экран."""
        hovered = QApplication.widgetAt(QCursor.pos())
        if hovered is not None:
            return hovered
        focused = QApplication.focusWidget()
        if focused is not None and focused is not self.menu:
            return focused
        return self.current_screen()

    def show_help(self, widget=None):
        if self.help_window is None:
            self.help_window = HelpWindow(self)
        return self.help_window.show_topic(help_id_of(widget or self.help_target()))

    def show_screen(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.screens[index].refresh()
        self.update_status()

    def request_stop(self) -> None:
        """Кнопка «Остановить»: остановить прогон, только если оператор подтвердил."""
        if self.ask_stop(self, False):
            self.run_control.stop()

    def closeEvent(self, event):  # noqa: N802 — имя метода задаёт Qt
        """Закрытие окна во время прогона — только с подтверждением и после протоколов (S13).

        Окно не закрывается сразу: рабочие процессы и выпуск протоколов живут в этом процессе,
        и закрытие оборвало бы их. Окно закроется само по сигналу о выпуске протоколов.
        """
        if not self.run_control.running:
            event.accept()
            return
        event.ignore()
        if self.close_after_run:
            return  # уже закрываемся: повторный крестик не мешает выпуску протоколов
        if self.ask_stop(self, True):
            if not self.run_control.running:  # прогон закончился, пока был открыт вопрос
                event.accept()
                return
            self.close_after_run = True
            self.run_control.stop()

    def _close_if_requested(self) -> None:
        if self.close_after_run:
            self.close()

    def open_protocols(self) -> None:
        """Перейти на экран «Протоколы»."""
        titles = [s.title for s in self.screens]
        self.menu.setCurrentRow(titles.index("Протоколы"))

    def current_screen(self):
        return self.screens[self.pages.currentIndex()]

    def update_status(self) -> None:
        stand = self.state.stand
        run = "идёт прогон" if self.state.running else "прогон не идёт"
        cameras = f"камер на местах: {len(stand.cameras)} из {MAX_SLOTS}"
        self.status.setText(f"Место стенда: {stand.place.title} · {cameras} · {run}")
        if hasattr(self, "run_block"):
            self.run_block.update_view()
