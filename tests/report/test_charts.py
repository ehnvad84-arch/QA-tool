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
