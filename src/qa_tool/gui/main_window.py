"""Главное окно: меню из пяти экранов слева, строка статуса стенда снизу."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

from qa_tool import __version__
from qa_tool.core.models import MAX_SLOTS
from qa_tool.gui.help import help_id_of, helped
from qa_tool.gui.help_window import HelpWindow
from qa_tool.gui.screens import SCREENS
from qa_tool.gui.state import AppState


class MainWindow(QMainWindow):
    def __init__(self, state: AppState | None = None):
        super().__init__()
        self.state = state or AppState()
        self.setWindowTitle(f"QA-tool {__version__} — стенд проверки IP-камер")
        self.resize(1280, 800)

        self.menu = helped(
            QListWidget(), "Разделы программы. Выберите раздел, чтобы открыть его.", "index"
        )
        self.menu.setFixedWidth(180)
        self.pages = QStackedWidget()
        self.screens = []
        for screen_cls in SCREENS:
            screen = screen_cls(self.state)
            self.screens.append(screen)
            self.pages.addWidget(screen)
            item = QListWidgetItem(screen.title)
            item.setToolTip(screen.hint)
            self.menu.addItem(item)
        self.menu.currentRowChanged.connect(self.show_screen)

        central = QWidget()
        row = QHBoxLayout(central)
        row.addWidget(self.menu)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(central)

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

    def current_screen(self):
        return self.screens[self.pages.currentIndex()]

    def update_status(self) -> None:
        stand = self.state.stand
        run = "идёт прогон" if self.state.running else "прогон не идёт"
        cameras = f"камер на местах: {len(stand.cameras)} из {MAX_SLOTS}"
        self.status.setText(f"Место стенда: {stand.place.title} · {cameras} · {run}")
