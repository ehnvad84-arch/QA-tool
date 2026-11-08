"""Графики отчётов: величина настройки по камерам в положениях 0/50/100 % («Картинка») и
кривые «качество — битрейт» на числовой оси («Кодеки»).

Рисуются средствами Qt (QPainter в PNG), как и PDF: без внешних программ, одинаково на Windows и
Linux. Отчёт печатают и в чёрно-белом виде, поэтому камеры различаются не только цветом, но и
типом линии и формой метки; цвета палитры — с контрастом к белому фону не меньше 4,5:1 (WCAG AA).
Подписей у линий нет — только легенда справа столбцом, поэтому на 8 камерах они не налезают.
"""

import math
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
THRESHOLD_COLOR = "#555555"  # 7,5:1 к белому: штрих порога виден и при печати


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
        points = [point(i, v) for i, v in enumerate(line.values) if v is not None]
        _draw_line(painter, points, number, DASHES[number], rect, line.label)
    painter.end()
    image.save(str(path), "PNG")
    return path


def _draw_line(
    painter: QPainter,
    points: list[QPointF],
    place: int,
    dash: Qt.PenStyle,
    rect: QRectF,
    label: str,
) -> None:
    """Линия с метками и её строка легенды: цвет и метка места `place`, тип линии `dash`."""
    pen = QPen(QColor(PALETTE[place]), 2.2, dash)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for a, b in zip(points, points[1:], strict=False):
        painter.drawLine(a, b)
    painter.setPen(QPen(QColor(PALETTE[place]), 1.6))
    painter.setBrush(QColor("white"))
    for p in points:
        _marker(painter, MARKERS[place], p)
    painter.setPen(pen)
    y = rect.center().y()
    painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() + 34, y))
    painter.setPen(QPen(QColor(PALETTE[place]), 1.6))
    _marker(painter, MARKERS[place], QPointF(rect.left() + 17, y))
    painter.setPen(QPen(QColor("#111111"), 1))
    painter.drawText(
        QRectF(rect.left() + 42, rect.top(), rect.width() - 42, rect.height()),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        label,
    )


@dataclass(frozen=True)
class XYSeries:
    """Кривая на числовой оси: подпись, точки (x, y) и место в палитре.

    `place` задаёт цвет и метку (`PALETTE`, `MARKERS`); `dash` — тип линии, по умолчанию — места.
    Так H.264 и H.265 одной камеры различаются местом, а «эта / прошлая прошивка» — линией.
    """

    label: str
    points: list[tuple[float, float]]
    place: int = 0
    dash: Qt.PenStyle | None = None


def xy_chart(
    path: Path,
    title: str,
    x_unit: str,
    y_unit: str,
    series: list[XYSeries],
    x_ticks: list[tuple[float, str]],
    y_range: tuple[float, float] = (0.0, 100.0),
    threshold: tuple[float, str] | None = None,
) -> Path:
    """Нарисовать кривые на логарифмической оси X в PNG.

    Ось Y — постоянная (`y_range`): графики разных профилей и камер сравниваются на глаз. Ось X —
    логарифмическая: ступени битрейта отличаются примерно вдвое, на линейной нижние слипаются.

    Args:
        path: куда сохранить.
        title: заголовок над графиком.
        x_unit: подпись оси X.
        y_unit: подпись оси Y.
        series: кривые, не больше 8; точки вне диапазона осей прижимаются к краю.
        x_ticks: деления оси X — (значение > 0, подпись), по возрастанию; крайние задают диапазон.
        y_range: диапазон оси Y.
        threshold: порог — штриховая линия `#555` по Y с подписью; None — без порога.

    Returns:
        Путь к файлу.
    """
    ensure_app()
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(QFont("DejaVu Sans", 9))
    x_low, x_high = math.log(x_ticks[0][0]), math.log(x_ticks[-1][0])
    if x_high - x_low < 1e-9:
        x_low, x_high = x_low - 1, x_high + 1
    y_low, y_high = y_range

    def point(x: float, y: float) -> QPointF:
        share = (math.log(max(x, 1e-9)) - x_low) / (x_high - x_low)
        height = (y - y_low) / (y_high - y_low)
        share, height = min(1.0, max(0.0, share)), min(1.0, max(0.0, height))
        return QPointF(PLOT.left() + share * PLOT.width(), PLOT.bottom() - height * PLOT.height())

    painter.setPen(QPen(QColor("#111111"), 1))
    painter.drawText(QRectF(0, 4, WIDTH, 20), Qt.AlignmentFlag.AlignCenter, title)
    painter.drawRect(PLOT)
    for value, label in x_ticks:
        x = point(value, y_low).x()
        painter.setPen(QPen(QColor("#cccccc"), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(x, PLOT.top()), QPointF(x, PLOT.bottom()))
        painter.setPen(QPen(QColor("#111111"), 1))
        painter.drawText(
            QRectF(x - 30, PLOT.bottom() + 4, 60, 16), Qt.AlignmentFlag.AlignCenter, label
        )
    painter.drawText(
        QRectF(PLOT.left(), PLOT.bottom() + 22, PLOT.width(), 18),
        Qt.AlignmentFlag.AlignCenter,
        x_unit,
    )
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = y_low + fraction * (y_high - y_low)
        y = point(x_ticks[0][0], value).y()
        painter.setPen(QPen(QColor("#cccccc"), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
        painter.setPen(QPen(QColor("#111111"), 1))
        painter.drawText(
            QRectF(0, y - 9, PLOT.left() - 6, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{value:g}".replace(".", ","),
        )
    painter.save()
    painter.translate(14, PLOT.center().y())
    painter.rotate(-90)
    painter.drawText(QRectF(-120, -10, 240, 20), Qt.AlignmentFlag.AlignCenter, y_unit)
    painter.restore()
    if threshold is not None:
        level, label = threshold
        y = point(x_ticks[0][0], level).y()
        painter.setPen(QPen(QColor(THRESHOLD_COLOR), 1.4, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
        painter.setPen(QPen(QColor(THRESHOLD_COLOR), 1))
        painter.drawText(
            QRectF(PLOT.left() + 6, y - 18, PLOT.width() - 12, 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            label,
        )
    for line, rect in zip(series[:8], legend_rects(len(series[:8])), strict=False):
        place = line.place % len(PALETTE)
        points = [point(x, y) for x, y in sorted(line.points)]
        _draw_line(painter, points, place, line.dash or DASHES[place], rect, line.label)
    painter.end()
    image.save(str(path), "PNG")
    return path
