"""Приложение «API камеры» в протоколе (api-discovery, T17): рамка блокера, сводка, сверка,
автопоиск, разделы; номер приложения; листы Excel «Сверка API» и «Пары API»; карта по трафику."""

import pytest
from openpyxl import load_workbook

from qa_tool.checks import load_all
from qa_tool.checks.api.discovery import CHECK_ID
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.api_map import LIBRARY_KEY, WEB_TRAFFIC_KEY
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.store import RunStore
from qa_tool.core.write_journal import API
from qa_tool.drivers.camera.browser import TrafficEntry
from qa_tool.report.html import render_protocol
from qa_tool.report.model import build_protocol
from qa_tool.report.xlsx import write_protocol_xlsx
from tests.mock_camera import MockCamera, Passport
from tests.mock_native import hikvision, side_effect_like_111

load_all()
SPEC = REGISTRY.get(CHECK_ID)
SECRET = "test-secret-17"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
FAST = {
    "api.apply_delay_s": Threshold(0.01, "с", "тест"),
    "api.baseline_gap_s": Threshold(0.01, "с", "тест"),
}
PASSPORT = {"manufacturer": "Hikvision", "model": "DS-2CD2043G2-I", "firmware": "V1", "serial": "S"}


def store_of(root, camera, result, allow_writes):
    stand = Stand(place=Place.COLOR_BOX, runs_dir=root / "runs", cameras=[camera])
    store = RunStore.create(stand, Scenario(checks=[SPEC.id], allow_writes=allow_writes))
    store.add_result(1, SPEC, result, "t")
    return store


@pytest.fixture(scope="module")
def blocked(tmp_path_factory):
    """Hikvision как .111: запись яркости сдвигает MaxGain, ONVIF его не возвращает, а PUT
    блока Gain камера отвергает — камера остаётся изменённой."""
    root = tmp_path_factory.mktemp("api")
    api = hikvision(password=SECRET)
    side_effect_like_111(api)
    api.state.imaging_sticky["MaxGain"] = "0"
    api.overrides["/ISAPI/Image/channels/1/gain"] = (403, "application/xml", b"<x/>")
    mock = MockCamera(
        Passport("Hikvision", "DS-2CD2043G2-I", "V1", "S"), password=SECRET, native=api
    )
    mock.start()
    try:
        camera = Camera(slot=1, address="127.0.0.1", port=mock.port, username="admin")
        extra = {
            PASSPORT_KEY: PASSPORT,
            LIBRARY_KEY: str(root / "lib"),
            API.folder_key: str(root / "pending"),
            "run_id": "QA-T17",
        }
        ctx = CheckContext(camera, SECRET, Place.COLOR_BOX, FAST, extra=extra, allow_writes=True)
        result = run_check(SPEC, ctx)
    finally:
        mock.stop()
    return store_of(root, camera, result, True)


def test_appendix_has_alarm_summary_compare_search_and_sections(blocked):
    protocol = build_protocol(blocked, 1)
    a = protocol.api
    assert a is not None and a.number == 1 and a.allowed and not a.traffic
    assert protocol.appendices == [(1, "API камеры: карта и сверка с ONVIF")]
    assert a.alarm and any("Gain/GainLevel" in line.param for line in a.left)
    stuck = next(line for line in a.left if "GainLevel" in line.param)
    assert (stuck.was, stuck.now) == ("100", "0") and "пробовала один раз" in stuck.how
    titles = dict(a.summary)
    assert titles["Родной API"].startswith("ISAPI") and "библиотека" not in titles
    assert "Библиотека" in titles and f"Итог {CHECK_ID}" in titles
    assert a.searches and a.searches[0].title == "Яркость"
    first = a.searches[0]
    assert first.writes.startswith("50 → ") and any("brightnessLevel" in m for m in first.moved)
    assert any(m.startswith("! в ONVIF: Exposure/MaxGain") for m in first.moved)
    assert a.sections and a.sections[0].section


def test_html_escapes_camera_text_and_includes_appendix(blocked):
    html = render_protocol(build_protocol(blocked, 1))
    assert "Приложение 1. API камеры: карта и сверка с ONVIF" in html
    assert "Камера осталась изменённой после автопоиска" in html
    assert "api_map_1.html" in html


def test_excel_has_match_and_pairs_sheets_with_numbers(blocked, tmp_path):
    protocol = build_protocol(blocked, 1)
    book = load_workbook(write_protocol_xlsx(tmp_path / "p.xlsx", protocol))
    match, pairs = book["Сверка API"], book["Пары API"]
    assert match["A1"].value == "ID" and match.auto_filter.ref
    numbers = [row[4].value for row in match.iter_rows(min_row=2) if row[4].value is not None]
    assert all(isinstance(v, int | float) for v in numbers)
    returns = [row[9].value for row in pairs.iter_rows(min_row=2)]
    assert any(r and r.startswith("не вернулось") for r in returns)


def test_traffic_map_appendix_has_no_compare(tmp_path):
    camera = Camera(slot=1, address="127.0.0.1", port=1, username="admin", vendor="generic")
    passport = {"manufacturer": "Tiandy", "model": "TC", "firmware": "1"}
    entry = TrafficEntry(
        "GET", "http://127.0.0.1:1/CGI/Image", 200, "application/json", b'{"a": 1}'
    )
    extra = {PASSPORT_KEY: passport, WEB_TRAFFIC_KEY: {"запросы": [entry], "остановлены": []}}
    result = run_check(SPEC, CheckContext(camera, SECRET, Place.COLOR_BOX, extra=extra))
    assert result.status is Status.PASSED
    protocol = build_protocol(store_of(tmp_path, camera, result, False), 1)
    assert protocol.api is not None and protocol.api.traffic
    html = render_protocol(protocol)
    assert "Карта снята по трафику веб-обхода" in html and "Сверка ONVIF" not in html
    book = load_workbook(write_protocol_xlsx(tmp_path / "p.xlsx", protocol))
    assert "Сверка API" not in book.sheetnames


def test_pdf_prints_the_appendix(blocked, tmp_path):
    from qa_tool.report.pdf import write_pdf

    html = render_protocol(build_protocol(blocked, 1))
    pdf = write_pdf(tmp_path / "p.pdf", html, "Протокол", blocked.run_dir)
    assert pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 10_000
