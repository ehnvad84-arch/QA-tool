"""Сборщик «Рувер» по OpenAPI-карте (api-discovery, T8): чтение в сеансе `RuverClient`,
чувствительные операции — только описание, диапазоны из ответа, возврат операцией карты;
`API-RUVER-1` ставит и уважает общий признак отказа входа."""

import json

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api.ruver import CHECK_ID
from qa_tool.checks.rtsp_refusal import refused_password, remember_web_refusal
from qa_tool.core.api_map import NativeMap, Sensitivity
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import RUVER, Camera, Place
from qa_tool.drivers.camera.native import NativeReader, ParseRefused, ReadTemplate
from qa_tool.drivers.camera.ruver.client import RuverClient
from qa_tool.drivers.camera.ruver.collector import RuverCollector, class_of, ruver_fetch
from tests.mock_ruver import MockRuver

load_all()
COLLECTOR = RuverCollector()
BRIGHTNESS = "/api/image/brightness?time_of_day=0"


@pytest.fixture
def camera():
    mock = MockRuver().start()
    yield mock
    mock.stop()


def place(mock) -> Camera:
    return Camera(slot=1, address="127.0.0.1", port=mock.port, username="admin", vendor=RUVER)


def read_all(mock) -> tuple[NativeMap, NativeReader]:
    client = RuverClient("127.0.0.1", mock.port, "admin", mock.password, timeout=2.0)
    with client:
        reader = NativeReader(place(mock), COLLECTOR, None, fetch=ruver_fetch(client, COLLECTOR))
        result = reader.read(NativeMap(brand="ruver", collector="OpenAPI", login="сеанс «Рувер»"))
    return result, reader


def test_map_of_virtual_ruver(camera):
    result, _ = read_all(camera)
    ids = {s.id for s in result.sections}
    assert BRIGHTNESS in ids and "/api/info" in ids
    brightness = result.params()[f"{BRIGHTNESS}#/brightness"]
    assert (brightness.type, brightness.allowed, brightness.value) == (
        "целое",
        {"мин": 0, "макс": 100},
        "50",
    )
    # диапазон стал допустимыми значениями соседа, а не отдельным параметром
    assert f"{BRIGHTNESS}#/brightnessRange/min" not in result.params()
    assert result.login == "сеанс «Рувер»"
    assert not result.cut


def test_sensitive_operations_are_described_not_read(camera):
    result, _ = read_all(camera)
    users = next(s for s in result.sections if s.id == "/api/users")
    assert users.sensitivity is Sensitivity.SENSITIVE and users.code is None
    assert "только описание" in users.read
    assert all(p.value is None for p in users.params) and users.params
    assert camera.sent("GET", "/api/users") == []
    assert camera.sent("GET", "/api/service/export_settings") == []
    assert [s.cls for s in result.sections if s.id == "/api/network/https"] == [
        "сеть и безопасность"
    ]


def test_live_operations_are_volatile(camera):
    result, _ = read_all(camera)
    ptz = [p for s in result.sections if s.id == "/api/ptz" for p in s.params]
    assert ptz and all(p.volatile for p in ptz)


def test_only_map_reads_reach_camera(camera):
    read_all(camera)
    methods = {(r.method, r.path) for r in camera.requests}
    posts = {path for method, path in methods if method != "GET"}
    assert posts <= {"/api/auth/login", "/api/auth/logout"}


def test_unknown_target_is_not_sent(camera):
    client = RuverClient("127.0.0.1", camera.port, "admin", camera.password, timeout=2.0)

    class Strange(RuverCollector):
        def reads(self, found):
            return [ReadTemplate("/api/service/reboot", "/api/service/reboot", "система")]

    with client:
        reader = NativeReader(place(camera), Strange(), None, fetch=ruver_fetch(client, COLLECTOR))
        result = reader.read(NativeMap(brand="ruver", collector="OpenAPI"))
    assert result.sections == [] and "не отправлен" in result.errors[0].reason
    assert camera.sent("GET", "/api/service/reboot") == []


@pytest.mark.parametrize(
    "body",
    [b'{"brightness": NaN}', b"[" * 100 + b"]" * 100, b"not json", b"\xff\xfe", b"x" * (3 << 20)],
)
def test_hostile_json_is_refused(body):
    template = ReadTemplate("/api/image/brightness", BRIGHTNESS, "картинка", query="time_of_day=0")
    with pytest.raises(ParseRefused):
        COLLECTOR.parse(template, body, "application/json", None)


def test_restore_brightness_with_map_operation(camera):
    m0, _ = read_all(camera)
    param = m0.params()[f"{BRIGHTNESS}#/brightness"]
    write = COLLECTOR.restore_request(param, m0)
    assert (write.method, write.path, write.query) == (
        "POST",
        "/api/image/brightness",
        "time_of_day=0",
    )
    assert json.loads(write.body) == {"brightness": 50}


def test_restore_only_image_settings(camera):
    m0, _ = read_all(camera)
    others = [
        p
        for s in m0.sections
        if class_of(s.id) not in ("картинка",)
        for p in s.params
        if p.value is not None
    ]
    assert others
    assert all(COLLECTOR.restore_request(p, m0) is None for p in others)


def test_class_of():
    assert class_of("/api/image/brightness") == "картинка"
    assert class_of("/api/video/stream") == "кодировщик"
    assert class_of("/api/users") == "сеть и безопасность"
    assert class_of("/api/glonass") == "прочее"


def ruver_ctx(mock, password) -> CheckContext:
    return CheckContext(place(mock), password, Place.COLOR_BOX, extra={"api_timeout_s": 0.3})


def test_ruver_check_sets_shared_refusal(camera):
    ctx = ruver_ctx(camera, "wrong-6")
    result = run_check(REGISTRY.get(CHECK_ID), ctx)
    assert result.status is Status.SKIPPED
    assert "родного API" in refused_password(ctx)


def test_ruver_check_does_not_log_in_after_refusal(camera):
    ctx = ruver_ctx(camera, camera.password)
    remember_web_refusal(ctx, "401")
    result = run_check(REGISTRY.get(CHECK_ID), ctx)
    assert result.status is Status.SKIPPED and "новых попыток входа нет" in result.reason
    assert camera.login_attempts == 0
