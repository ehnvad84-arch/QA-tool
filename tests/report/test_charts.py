"""График сравнения картинки (этап 7, T16): контраст палитры, чёрно-белая печать, легенда."""

from qa_tool.report import charts


def test_palette_contrast_to_white_is_at_least_aa():
    for color in charts.PALETTE:
        assert charts.contrast_to_white(color) >= 4.5, color


def test_cameras_differ_without_colour():
    styles = list(zip(charts.DASHES, charts.MARKERS, strict=True))
    assert len(set(styles)) == len(styles) == 8


def test_legend_rows_of_eight_cameras_do_not_overlap():
    rects = charts.legend_rects(8)
    for a, b in zip(rects, rects[1:], strict=False):
        assert not a.intersects(b)
    assert rects[-1].bottom() <= charts.HEIGHT


def test_chart_for_eight_cameras_is_drawn(tmp_path):
    series = [
        charts.Series(f"М{i}", [10.0 * i, 12.0 * i, None if i == 3 else 15.0 * i])
        for i in range(1, 9)
    ]
    path = charts.line_chart(
        tmp_path / "c.png", "Яркость", "уровень", ["0 %", "50 %", "100 %"], series
    )
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 5000
    flat = charts.line_chart(
        tmp_path / "f.png", "Плоско", "уровень", ["выкл", "вкл"], [charts.Series("М1", [5.0, 5.0])]
    )
    assert flat.exists()


def test_threshold_colour_contrast_is_at_least_aa():
    assert charts.contrast_to_white(charts.THRESHOLD_COLOR) >= 7.0


def test_xy_chart_puts_points_on_log_axis_and_draws_threshold(tmp_path):
    from PySide6.QtGui import QColor, QImage

    ticks = [(256.0, "0,25"), (1024.0, "1"), (4096.0, "4"), (16384.0, "16")]
    series = [
        charts.XYSeries("H.264", [(512.0, 71.3), (2048.0, 88.0), (8192.0, 96.8)], place=0),
        charts.XYSeries("H.265", [(512.0, 80.0), (100000.0, 99.0)], place=1),  # вне оси — у края
    ]
    path = charts.xy_chart(
        tmp_path / "q.png",
        "Профиль 1",
        "Битрейт, Мбит/с (логарифмическая шкала)",
        "VMAF",
        series,
        ticks,
        threshold=(70.0, "порог 1920×1080: 70"),
    )
    image = QImage(str(path))
    plot = charts.PLOT
    # 1024 кбит/с — ровно треть логарифмической оси 256…16384
    third = round(plot.left() + plot.width() / 3)
    y70 = round(plot.bottom() - 0.7 * plot.height())
    span = range(int(plot.left()) + 2, int(plot.right()) - 2)
    # штрих порога сглажен на две строки пикселей: серый, темнее сетки
    grey = [
        sum(1 for x in span if (c := image.pixelColor(x, y)).red() < 170 and c.red() == c.blue())
        for y in (y70 - 1, y70)
    ]
    assert max(grey) > 200, grey
    column = [image.pixelColor(third, y) for y in range(int(plot.top()), int(plot.bottom()))]
    assert any(c != QColor("white") for c in column)  # деление «1» на трети оси
