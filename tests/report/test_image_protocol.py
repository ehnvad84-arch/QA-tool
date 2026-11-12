"""Приложение «Картинка» в протоколе (этап 7, T14): снимки, порог с источником, экранирование,
PDF со снимками."""

import pytest

from qa_tool.checks.image import engine
from qa_tool.core.checks import CheckContext, run_check
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.store import RunStore
from qa_tool.report.html import render_protocol
from qa_tool.report.model import build_protocol
from qa_tool.report.pdf import write_pdf
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.checks.test_image_effect import FAST, MANUAL, SPEC
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import IMAGING_OPTIONS, OnvifState

pytestmark = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None or tool_path("ffprobe") is None, reason="нет ffmpeg"
)


@pytest.fixture
def stored(tmp_path):
    hostile = IMAGING_OPTIONS.replace(
        "<tt:Sharpness><tt:Min>0</tt:Min>", "<tt:Sharpness><tt:Min>&lt;b&gt;x</tt:Min>"
    )
    cam = MockCamera(
        Passport("X", "M", "1", "S1"),
        state=OnvifState(
            imaging_xml=MANUAL,
            imaging_video=True,
            imaging_options_xml=hostile,
            imaging_effects={"saturation": "none"},
        ),
    ).start()
    try:
        camera = Camera(
            slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
        )
        limits = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
        ctx = CheckContext(camera, "admin", Place.COLOR_BOX, limits, allow_writes=True)
        ctx.extra[engine.SETTINGS_KEY] = ["brightness", "saturation", "sharpness"]
        result = run_check(SPEC, ctx)
    finally:
        cam.stop()
    stand = Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=[camera])
    store = RunStore.create(stand, Scenario(checks=[SPEC.id], allow_writes=True))
    store.add_result(1, SPEC, result, "t")
    return store


def test_appendix_has_three_snapshots_per_setting_and_threshold_source(stored):
    protocol = build_protocol(stored, 1)
    appendix = protocol.image
    assert appendix is not None and appendix.allowed and appendix.exposure == "MANUAL"
    bright = next(b for b in appendix.blocks if b.check_id == "IMAGE-BRIGHT-1")
    assert [s.label for s in bright.shots] == ["0 %", "50 %", "100 %"]
    assert all((stored.run_dir / s.thumb).exists() for s in bright.shots)
    assert "(стартовое значение" in bright.threshold and bright.side == "растёт"
    deaf = next(b for b in appendix.blocks if b.check_id == "IMAGE-SAT-1")
    assert deaf.status == "не прошла" and any(s.weak for s in deaf.shots)
    assert appendix.restore_status == "прошла" and appendix.state_image
    row = next(r for r in protocol.rows if r.check_id == "IMAGE-BRIGHT-1")
    assert row.actual.startswith("0 % — ") and "средняя яркость кадра" in row.actual


def test_camera_text_is_escaped_and_pdf_builds_with_images(stored):
    protocol = build_protocol(stored, 1)
    html = render_protocol(protocol)
    assert "Приложение 1. Картинка: снимки и замеры" in html
    assert "<b>x" not in html and "&lt;b&gt;x" in html  # диапазон резкости — ответ камеры
    assert 'src="evidence/1/IMAGE-BRIGHT-1/p050_t.jpg"' in html
    plain = write_pdf(stored.run_dir / "plain.pdf", html, "без базы")
    with_images = write_pdf(stored.run_dir / "p.pdf", html, "протокол", stored.run_dir)
    assert with_images.stat().st_size > plain.stat().st_size + 10_000  # снимки внутри


def test_excel_sheet_links_snapshots_and_has_no_formulas(stored):
    from openpyxl import load_workbook

    from qa_tool.report.xlsx import write_protocol_xlsx

    path = write_protocol_xlsx(stored.run_dir / "protocol_1.xlsx", build_protocol(stored, 1))
    ws = load_workbook(path)["Картинка"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][0] == "IMAGE-STATE-1" and rows[1][5] == "MANUAL"
    by_id = {r[0]: r for r in rows[2:]}
    bright = by_id["IMAGE-BRIGHT-1"]
    assert bright[2] == "Brightness" and bright[3] == "0,0…100,0" and bright[16] == "прошла"
    assert bright[6].startswith("0,0 / 0,0")
    links = [c.hyperlink.target for c in ws[3] if c.hyperlink]
    assert len(links) == 3 and all((stored.run_dir / link).exists() for link in links)
    assert not any(isinstance(v, str) and v.startswith("=") for row in rows for v in row)
    assert by_id["IMAGE-SHARP-1"][16] == "не прошла"  # неверный диапазон — строка без снимков
