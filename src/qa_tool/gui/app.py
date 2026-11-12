"""Запуск окна."""

import datetime
import sys

from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory

from qa_tool.core.models import ConfigError
from qa_tool.gui import theme
from qa_tool.gui.help import ToolTipStyle
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.state import AppState, default_stand_path


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    # Fusion одинаково слушается палитры на Windows и Linux; родной стиль Windows
    # тёмную палитру частично игнорирует (светлые кнопки и поля в тёмной теме).
    app.setStyle(ToolTipStyle(QStyleFactory.create("Fusion")))
    app.setApplicationName("QA-tool")
    theme.load_fonts()
    theme.apply(app, theme.saved_theme())
    return app


def load_state() -> tuple[AppState, str]:
    """Состояние окна; испорченный stand.yaml откладывается в сторону, а не роняет программу.

    Returns:
        Состояние и сообщение для инженера (пустое, если всё прочиталось).
    """
    path = default_stand_path()
    try:
        return AppState(stand_path=path), ""
    except ConfigError as e:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        broken = path.with_name(f"{path.name}.broken-{stamp}")
        path.rename(broken)
        message = (
            f"Файл стенда не прочитан: {e}\n\nОн сохранён как {broken.name}, "
            "программа открыта с пустым стендом. Камеры нужно расставить заново."
        )
        return AppState(stand_path=path), message


def run_gui(state: AppState | None = None) -> int:
    app = create_app()
    message = ""
    if state is None:
        state, message = load_state()
    window = MainWindow(state)
    window.show()
    if message:
        QMessageBox.warning(window, "QA-tool", message)
    return app.exec()
