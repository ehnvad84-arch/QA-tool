"""Приложение «Кодеки» в протоколе (этап 8, T14): лист кодировщика с графиком, ступени,
итоги кодеков, номер приложения после «Картинки», PDF с графиком."""

import pytest

from qa_tool.checks.codec.quality import has_libvmaf
from qa_tool.core.checks import CheckContext, run_check
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.store import RunStore
from qa_tool.report.codec import _better, mbit, rate_ticks
from qa_tool.report.html import render_protocol
from qa_tool.report.model import build_protocol
from qa_tool.report.pdf import write_pdf
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.checks.test_codec_engine import FAST, SPEC
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

needs_vmaf = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None
    or tool_path("ffprobe") is None
    or not has_libvmaf(tool_path("ffmpeg") or ""),
    reason="нет ffmpeg с libvmaf",
)


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    cam = MockCamera(
        Passport("X", "M", "1", "S1"),
        state=OnvifState(encoder_video=True, encoder_codecs=("H264",), encoder_kbps_factor=2.0),
    ).start()
    try:
        camera = Camera(
            slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
        )
        limits = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
        ctx = CheckContext(camera, "admin", Place.COLOR_BOX, limits, allow_writes=True)
        result = run_check(SPEC, ctx)
    finally:
        cam.stop()
    root = tmp_path_factory.mktemp("codec")
    stand = Stand(place=Place.COLOR_BOX, runs_dir=root / "runs", cameras=[camera])
    store = RunStore.create(stand, Scenario(checks=[SPEC.id], allow_writes=True))
    store.add_result(1, SPEC, result, "t")
    return store


def test_mbit_and_ticks_cover_points():
    assert (mbit(512), mbit(2048), mbit(64)) == ("0,5", "2", "0,06")
    ticks = rate_ticks([(600.0, 70.0), (4100.0, 95.0)])
    assert ticks[0][0] <= 600 and ticks[-1][0] >= 4100 and len(ticks) >= 3


def test_better_codec_sentence_counts_common_steps():
    def steps(values):
        return {
            "ступени": [{"задано": k, "качество": {"vmaf": [v, 0, v]}} for k, v in values.items()]
        }

    found = _better({"H264": steps({1024: 70, 2048: 80}), "H265": steps({1024: 80, 2048: 88})}, 2)
    assert found == "При равном битрейте лучше H.265: на всех ступенях, в среднем на 9,0 VMAF."
    same = _better({"H264": steps({1024: 80}), "H265": steps({1024: 81})}, 2)
    assert "равны в пределах разброса" in same


@needs_vmaf
def test_sheet_has_chart_steps_and_failed_bitrate(stored):
    protocol = build_protocol(stored, 1)
    appendix = protocol.codec
    assert appendix is not None and appendix.number == 1 and appendix.allowed
    assert protocol.appendices == [(1, "Кодеки: качество на битрейт")]
    sheet = appendix.sheets[0]
    assert sheet.heading.startswith("Профиль 1 · ") and "320×240" in sheet.heading
    assert sheet.chart and (stored.run_dir / sheet.chart).read_bytes()[:4] == b"\x89PNG"
    assert [v.codec for v in sheet.verdicts] == ["H.264"]
    assert sheet.verdicts[0].status == "не прошла"
    assert any(step.bad_rate for step in sheet.steps)  # поток вдвое толще заданного
    assert all(step.vmaf != "—" for step in sheet.steps)
    assert sheet.shots and all((stored.run_dir / thumb).exists() for thumb, _f, _c in sheet.shots)
    skipped = [title for title, _status, _reason in appendix.without]
    assert any("H.265" in title for title in skipped)
    assert appendix.restore_status == "прошла" and appendix.state_images
    row = next(r for r in protocol.rows if r.check_id == "CODEC-H264-1")
    assert "Мбит/с → VMAF" in row.actual


@needs_vmaf
def test_html_and_pdf_carry_the_chart(stored):
    protocol = build_protocol(stored, 1)
    html = render_protocol(protocol)
    assert "Приложение 1. Кодеки: качество на битрейт" in html
    assert "Приложения: 1 — Кодеки: качество на битрейт." in html
    assert f'src="{protocol.codec.sheets[0].chart}"' in html
    assert 'class="bad">! ' in html  # нарушение — не только цветом
    plain = write_pdf(stored.run_dir / "plain.pdf", html, "без базы")
    with_images = write_pdf(stored.run_dir / "p.pdf", html, "протокол", stored.run_dir)
    assert with_images.stat().st_size > plain.stat().st_size + 10_000


@needs_vmaf
def test_excel_sheet_has_numbers_and_links_without_formulas(stored):
    from openpyxl import load_workbook

    from qa_tool.report.xlsx import write_protocol_xlsx

    path = write_protocol_xlsx(stored.run_dir / "protocol_1.xlsx", build_protocol(stored, 1))
    ws = load_workbook(path)["Кодеки"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][0] == "CODEC-STATE-1"
    steps = [r for r in rows if r[0] == "CODEC-H264-1"]
    assert len(steps) == 2 and steps[0][6] == "1 из 2" and steps[0][7] == 2048
    assert all(isinstance(r[10], float) and isinstance(r[8], float) for r in steps)
    assert any(r[16] == "не прошла" for r in steps)  # битрейт вдвое больше заданного
    first = next(i for i, r in enumerate(rows, start=1) if r[0] == "CODEC-H264-1")
    assert ws.cell(first, 11).number_format == "0.0"
    links = [c.hyperlink.target for c in ws[first] if c.hyperlink]
    assert len(links) == 2 and all((stored.run_dir / link).exists() for link in links)
    assert not any(isinstance(v, str) and v.startswith("=") for row in rows for v in row)
