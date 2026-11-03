"""Запуск окна."""

import sys

from PySide6.QtWidgets import QApplication

from qa_tool.gui.help import ToolTipStyle
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.state import AppState


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    app.setStyle(ToolTipStyle(app.style()))
    app.setApplicationName("QA-tool")
    return app


def run_gui(state: AppState | None = None) -> int:
    app = create_app()
    window = MainWindow(state)
    window.show()
    return app.exec()
