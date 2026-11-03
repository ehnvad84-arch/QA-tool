"""PDF из того же HTML средствами Qt: одинаково на Windows и Linux, без внешних программ."""

import os
from pathlib import Path

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument

_app: QGuiApplication | None = None


def _ensure_app() -> None:
    """В прогоне без окна поднимаем Qt без экрана."""
    global _app
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QGuiApplication([])


def write_pdf(path: Path, html: str, title: str) -> Path:
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
