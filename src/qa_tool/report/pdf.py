"""PDF из того же HTML средствами Qt: одинаково на Windows и Linux, без внешних программ."""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument

_app: QGuiApplication | None = None


def needs_offscreen() -> bool:
    """Без экрана Qt запускаем только на Linux без дисплея: на Windows в режиме offscreen
    у Qt нет шрифтов и PDF выходит без текста, а рабочий стол на Windows есть всегда."""
    return sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _ensure_app() -> None:
    """В прогоне без окна поднимаем Qt сами."""
    global _app
    if QGuiApplication.instance() is None:
        if needs_offscreen():
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QGuiApplication([])


def write_pdf(path: Path, html: str, title: str) -> Path:
    """Сохранить HTML как PDF (A4, альбомная) средствами Qt.

    На Linux без дисплея Qt поднимается без экрана, на Windows — в обычном режиме:
    в режиме offscreen у Qt на Windows нет шрифтов и PDF выходит пустым.
    """
    _ensure_app()
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("qa-tool")
    writer.setResolution(150)
    writer.setPageLayout(
        QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Landscape,
            QMarginsF(12, 12, 12, 12),
            QPageLayout.Unit.Millimeter,
        )
    )
    document = QTextDocument()
    document.setHtml(html)
    document.print_(writer)
    return path
