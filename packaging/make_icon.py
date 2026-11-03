"""Нарисовать значок программы: icon.png (Linux) и icon.ico (Windows) в указанный каталог."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402


def draw(size: int = 256) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#1f4e79"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(8, 8, size - 16, size - 16), size * 0.18, size * 0.18)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QRectF(size * 0.22, size * 0.14, size * 0.56, size * 0.56))
    p.setBrush(QColor("#1f4e79"))
    p.drawEllipse(QRectF(size * 0.34, size * 0.26, size * 0.32, size * 0.32))
    p.setPen(QColor("#ffffff"))
    font = QFont("DejaVu Sans", int(size * 0.17))
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(0, size * 0.68, size, size * 0.28), Qt.AlignmentFlag.AlignCenter, "QA")
    p.end()
    return image


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])
    image = draw()
    image.save(str(out / "icon.png"))
    image.scaled(
        64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    ).save(str(out / "icon.ico"))
