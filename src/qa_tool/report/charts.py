"""Графики отчёта сравнения картинки: величина настройки по камерам в положениях 0/50/100 %.

Рисуются средствами Qt (QPainter в PNG), как и PDF: без внешних программ, одинаково на Windows и
Linux. Отчёт печатают и в чёрно-белом виде, поэтому камеры различаются не только цветом, но и
типом линии и формой метки; цвета палитры — с контрастом к белому фону не меньше 4,5:1 (WCAG AA).
Подписей у линий нет — только легенда справа столбцом, поэтому на 8 камерах они не налезают.
"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF

from qa_tool.report.pdf import ensure_app

# Цвета мест 1…8: тёмные насыщенные, различимы между собой и при дальтонизме по типу линии
PALETTE = ("#1f4e9c", "#a12a2a", "#1b6b3a", "#7a3d99", "#8a5200", "#0f6b73", "#5b5b5b", "#b0127a")
DASHES = (
    Qt.PenStyle.SolidLine,
    Qt.PenStyle.DashLine,
    Qt.PenStyle.DotLine,
    Qt.PenStyle.DashDotLine,
    Qt.PenStyle.SolidLine,
    Qt.PenStyle.DashLine,
    Qt.PenStyle.DotLine,
    Qt.PenStyle.DashDotDotLine,
)
MARKERS = ("круг", "квадрат", "треугольник", "ромб", "крест", "круг", "квадрат", "треугольник")
WIDTH, HEIGHT = 900, 360
PLOT = QRectF(70, 30, 600, 280)  # область графика; справа — легенда
LEGEND_X, LEGEND_ROW = 700.0, 30.0


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast_to_white(hex_color: str) -> float:
    """Контраст цвета к белому по WCAG 2.1."""
    color = QColor(hex_color)
    luminance = (
        0.2126 * _channel(color.red())
        + 0.7152 * _channel(color.green())
        + 0.0722 * _channel(color.blue())
    )
    return 1.05 / (luminance + 0.05)


@dataclass(frozen=True)
class Series:
    """Линия одной камеры: подпись в легенде (М1…М8) и величины по положениям (None — нет)."""

    label: str
    values: list[float | None]


def legend_rects(count: int) -> list[QRectF]:
    """Где стоят строки легенды: тест проверяет, что они не налезают друг на друга."""
    return [
        QRectF(LEGEND_X, PLOT.top() + i * LEGEND_ROW, 180, LEGEND_ROW - 4) for i in range(count)
    ]


def _marker(painter: QPainter, kind: str, at: QPointF, size: float = 5.0) -> None:
    if kind == "круг":
        painter.drawEllipse(at, size, size)
    elif kind == "квадрат":
        painter.drawRect(QRectF(at.x() - size, at.y() - size, 2 * size, 2 * size))
    elif kind == "треугольник":
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(at.x(), at.y() - size),
                    QPointF(at.x() - size, at.y() + size),
                    QPointF(at.x() + size, at.y() + size),
                ]
            )
        )
    elif kind == "ромб":
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(at.x(), at.y() - size),
                    QPointF(at.x() + size, at.y()),
                    QPointF(at.x(), at.y() + size),
                    QPointF(at.x() - size, at.y()),
                ]
            )
        )
    else:
        painter.drawLine(
            QPointF(at.x() - size, at.y() - size), QPointF(at.x() + size, at.y() + size)
        )
        painter.drawLine(
            QPointF(at.x() - size, at.y() + size), QPointF(at.x() + size, at.y() - size)
        )


def line_chart(
    path: Path, title: str, unit: str, x_labels: list[str], series: list[Series]
) -> Path:
    """Нарисовать график в PNG.

    Args:
        path: куда сохранить.
        title: заголовок над графиком.
        unit: подпись оси величины.
        x_labels: подписи положений («0 %», «50 %», «100 %» или «выкл», «вкл»).
        series: линии камер, не больше 8.

    Returns:
        Путь к файлу.
    """
    ensure_app()
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(QFont("DejaVu Sans", 9))
    known = [v for s in series for v in s.values if v is not None]
    low, high = (min(known), max(known)) if known else (0.0, 1.0)
    if high - low < 1e-9:
        low, high = low - 1, high + 1
    pad = (high - low) * 0.08
    low, high = low - pad, high + pad

    def point(index: int, value: float) -> QPointF:
        step = PLOT.width() / max(1, len(x_labels) - 1)
        y = PLOT.bottom() - (value - low) / (high - low) * PLOT.height()
        return QPointF(PLOT.left() + index * step, y)

    painter.setPen(QPen(QColor("#111111"), 1))
    painter.drawText(QRectF(0, 4, WIDTH, 20), Qt.AlignmentFlag.AlignCenter, title)
    painter.drawRect(PLOT)
    for i, label in enumerate(x_labels):
        x = point(i, low).x()
        painter.drawText(
            QRectF(x - 40, PLOT.bottom() + 6, 80, 18), Qt.AlignmentFlag.AlignCenter, label
        )
    for fraction in (0.0, 0.5, 1.0):
        value = low + fraction * (high - low)
        y = point(0, value).y()
        painter.setPen(QPen(QColor("#cccccc"), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
        painter.setPen(QPen(QColor("#111111"), 1))
        painter.drawText(
            QRectF(0, y - 9, PLOT.left() - 6, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{value:.1f}".replace(".", ","),
        )
    painter.save()
    painter.translate(14, PLOT.center().y())
    painter.rotate(-90)
    painter.drawText(QRectF(-120, -10, 240, 20), Qt.AlignmentFlag.AlignCenter, unit)
    painter.restore()
    for number, (line, rect) in enumerate(
        zip(series[:8], legend_rects(len(series[:8])), strict=False)
    ):
        pen = QPen(QColor(PALETTE[number]), 2.2, DASHES[number])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        points = [point(i, v) for i, v in enumerate(line.values) if v is not None]
        for a, b in zip(points, points[1:], strict=False):
            painter.drawLine(a, b)
        painter.setPen(QPen(QColor(PALETTE[number]), 1.6))
        painter.setBrush(QColor("white"))
        for p in points:
            _marker(painter, MARKERS[number], p)
        painter.setPen(pen)
        y = rect.center().y()
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() + 34, y))
        painter.setPen(QPen(QColor(PALETTE[number]), 1.6))
        _marker(painter, MARKERS[number], QPointF(rect.left() + 17, y))
        painter.setPen(QPen(QColor("#111111"), 1))
        painter.drawText(
            QRectF(rect.left() + 42, rect.top(), rect.width() - 42, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            line.label,
        )
    painter.end()
    image.save(str(path), "PNG")
    return path
