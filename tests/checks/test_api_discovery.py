"""Проверка API-MAP-1 (api-discovery, T13): карта и сверка без записи на виртуальных камерах,
отказ входа одной попыткой, бренды без сборщика, пороги."""

import json

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api.discovery import CHECK_ID, NOT_FOUND
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.checks.rtsp_refusal import refused_password
from qa_tool.core.checks import REGISTRY, CheckContext, Phase, Severity, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from tests.mock_camera import MockCamera, Passport
from tests.mock_native import dahua, hikvision
from tests.mock_ruver import MockRuver

load_all()
SPEC = REGISTRY.get(CHECK_ID)
SECRET = "test-secret-9"  # выдуманный: настоящие пароли стенда в репозиторий не попадают


@pytest.fixture
def fleet():
    started = []

    def start(api, maker="Hikvision", model="DS-2CD2043G2-I"):
        camera = MockCamera(Passport(maker, model, "V5.7.3", "SN1"), password=SECRET, native=api)
        started.append(camera.start())
        return camera

    yield start
    for camera in started:
        camera.stop()


def ctx_for(
    port, maker="Hikvision", model="DS-2CD2043G2-I", password=SECRET, vendor="generic", **kw
):
    camera = Camera(slot=1, address="127.0.0.1", port=port, username="admin", vendor=vendor)
    passport = {"manufacturer": maker, "model": model, "firmware": "V5.7.3", "serial": "SN1"}
    return CheckContext(camera, password, Place.COLOR_BOX, extra={PASSPORT_KEY: passport}, **kw)


def children(result):
    return {c.check_id: c for c in result.children}


def test_registration():
    assert SPEC.phase is Phase.NETWORK and SPEC.order == 950
    assert not SPEC.writes and SPEC.vendors is None and SPEC.help_id == "api-map"
    assert SPEC.timeout_s == 1440


def test_hikvision_map_and_compare(fleet):
    api = hikvision(password=SECRET)
    camera = fleet(api)
    result = run_check(SPEC, ctx_for(camera.port))
    assert result.status is Status.PASSED, result.reason
    assert result.actual.startswith("карта снята: ISAPI, разделов")
    rows = children(result)
    assert rows["API-MATCH-12"].result.status is Status.PASSED
    # у виртуальной ONVIF нет диапазона MaxGain в GetOptions — шкалу «по диапазонам» не построить
    assert rows["API-MATCH-13"].result.status is Status.SKIPPED
    files = {e.name: e.content for e in result.evidence}
    native = json.loads(files["api-map.json"])
    assert native["камера"]["модель"] == "DS-2CD2043G2-I" and native["вход"] == "Digest MD5"
    assert SECRET not in files["api-map.json"] and SECRET not in files["exchanges.txt"]
    assert not any(r.startswith(("PUT", "POST", "DELETE")) for r in api.requests)


def test_exposure_mismatch_like_111_is_a_task(fleet):
    api = hikvision(password=SECRET, pinned={"Image/channels/1/Exposure/ExposureType": "manual"})
    camera = fleet(api)
    row = children(run_check(SPEC, ctx_for(camera.port)))["API-MATCH-12"]
    assert row.result.status is Status.FAILED and row.severity is Severity.MAJOR
    assert "ONVIF Imaging/Exposure/Mode = AUTO" in row.result.actual
    assert "ISAPI Image/channels/1/Exposure/ExposureType = manual" in row.result.actual


def test_refused_login_is_one_attempt_per_run(fleet):
    api = hikvision(password=SECRET)
    camera = fleet(api)
    ctx = ctx_for(camera.port, password="wrong-9")
    first = run_check(SPEC, ctx)
    assert first.status is Status.STAND_ERROR and "вход в родной API отвергнут" in first.reason
    assert api.refusals == 1 and refused_password(ctx)
    second = run_check(SPEC, ctx)
    assert second.status is Status.STAND_ERROR and "не пробовали" in second.reason
    assert api.refusals == 1


def test_dahua_without_pairs_gives_one_skipped_match(fleet):
    camera = fleet(dahua(password=SECRET), maker="Dahua", model="IPC-HFW2431S-S-S2")
    result = run_check(SPEC, ctx_for(camera.port, maker="Dahua", model="IPC-HFW2431S-S-S2"))
    assert result.status is Status.PASSED and "CGI" in result.actual
    rows = children(result)
    assert (
        list(rows) == ["API-MAP-1", "API-MATCH-0"]
        and rows["API-MATCH-0"].result.status is Status.SKIPPED
    )


@pytest.mark.parametrize(
    ("maker", "vendor", "reason"),
    [("Tiandy", "generic", "Tiandy"), ("Axis", "axis", NOT_FOUND), ("", "generic", NOT_FOUND)],
)
def test_brands_without_reader_are_skipped(maker, vendor, reason):
    result = run_check(SPEC, ctx_for(1, maker=maker, vendor=vendor))
    assert result.status is Status.SKIPPED and reason in result.reason


def test_brand_without_reader_maps_web_walk_traffic():
    from qa_tool.core.api_map import WEB_TRAFFIC_KEY
    from qa_tool.drivers.camera.browser import StoppedRequest, TrafficEntry, Verdict

    ctx = ctx_for(1, maker="Tiandy", model="TC-C34", vendor="generic")
    base = "http://127.0.0.1:1"
    ctx.extra[WEB_TRAFFIC_KEY] = {
        "запросы": [
            TrafficEntry(
                "GET",
                base + "/CGI/Image",
                200,
                "application/xml",
                b"<Image><Brightness>50</Brightness></Image>",
            ),
            TrafficEntry(
                "GET",
                base + "/CGI/Users",
                200,
                "application/xml",
                b"<Users><Name>admin</Name><Password>p1</Password></Users>",
            ),
        ],
        "остановлены": [StoppedRequest("POST", base + "/CGI/Network", Verdict.STOP_WRITE)],
    }
    result = run_check(SPEC, ctx)
    assert result.status is Status.PASSED, result.reason
    assert result.actual.startswith("карта по трафику веб-обхода: разделов 2")
    assert "сверки нет" in result.actual and result.children == []  # S2: без сверки и поиска
    data = json.loads(next(e.content for e in result.evidence if e.name == "api-map.json"))
    text = json.dumps(data, ensure_ascii=False)
    assert "трафик веб-обхода" in text and "p1" not in text
    assert "остановлена табу" in text


def test_walk_traffic_without_api_data_is_skipped_with_reason():
    from qa_tool.core.api_map import WEB_TRAFFIC_KEY
    from qa_tool.drivers.camera.browser import TrafficEntry

    ctx = ctx_for(1, maker="Axis", vendor="axis")
    page = TrafficEntry("GET", "http://127.0.0.1:1/", 200, "text/html", b"")
    ctx.extra[WEB_TRAFFIC_KEY] = {"запросы": [page], "остановлены": []}
    result = run_check(SPEC, ctx)
    assert result.status is Status.SKIPPED and "не увидел запросов" in result.reason


def test_bad_thresholds_are_stand_error():
    ctx = ctx_for(1, thresholds={"api.share_low": Threshold(0.9, "доля", "тест")})
    result = run_check(SPEC, ctx)
    assert result.status is Status.STAND_ERROR and "thresholds.yaml" in result.reason
    ctx = ctx_for(1, thresholds={"api.map_budget_s": Threshold(0, "с", "тест")})
    assert "api.map_budget_s" in run_check(SPEC, ctx).reason


def test_ruver_map_through_its_session():
    mock = MockRuver(password=SECRET).start()
    try:
        result = run_check(SPEC, ctx_for(mock.port, maker="Ruware", model="RV-1"))
    finally:
        mock.stop()
    assert result.status is Status.PASSED and "API «Рувер»" in result.actual
    # у виртуального «Рувера» нет ONVIF: сверке не с чем сравнивать, причина — в строке
    assert "ONVIF" in result.actual
    assert mock.closed_sessions  # сеанс закрыт: камера держит не больше трёх
