"""PDF из того же HTML средствами Qt: одинаково на Windows и Linux, без внешних программ."""

import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QMarginsF, QUrl
from PySide6.QtGui import (
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPdfWriter,
    QTextDocument,
)
from PySide6.QtWidgets import QApplication

_app: QApplication | None = None


def needs_offscreen() -> bool:
    """Без экрана Qt запускаем только на Linux без дисплея: на Windows в режиме offscreen
    у Qt нет шрифтов и PDF выходит без текста, а рабочий стол на Windows есть всегда."""
    return sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def ensure_app() -> None:
    """Поднять Qt для рисования без окна (прогон из командной строки): PDF и графики."""
    global _app
    if QGuiApplication.instance() is None:
        if needs_offscreen():
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # QApplication, а не QGuiApplication: в одном процессе с протоколом может потом
        # открыться окно (тесты, окно программы), а заменить уже созданное приложение Qt нельзя
        _app = QApplication([])


_IMG_SRC = re.compile(r'<img[^>]*\bsrc="([^"]+)"')


def _add_images(document: QTextDocument, html: str, base_dir: Path) -> None:
    """Подложить документу снимки из папки прогона: сам QTextDocument при печати их не грузит.

    Берутся только файлы внутри `base_dir`: путь в шаблоне строит программа, но и он не должен
    вывести за пределы папки прогона.
    """
    root = base_dir.resolve()
    for src in set(_IMG_SRC.findall(html)):
        path = (root / src).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        image = QImage(str(path))
        if not image.isNull():
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(src), image)


def write_pdf(path: Path, html: str, title: str, base_dir: Path | None = None) -> Path:
    """Сохранить HTML как PDF (A4, альбомная) средствами Qt.

    На Linux без дисплея Qt поднимается без экрана, на Windows — в обычном режиме:
    в режиме offscreen у Qt на Windows нет шрифтов и PDF выходит пустым.

    Args:
        path: куда сохранить.
        html: документ.
        title: заголовок PDF.
        base_dir: папка, от которой отсчитаны относительные пути картинок (снимки картинки);
            None — картинок в документе нет.
    """
    ensure_app()
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
    if base_dir is not None:
        _add_images(document, html, base_dir)
    document.setHtml(html)
    document.print_(writer)
    return path
