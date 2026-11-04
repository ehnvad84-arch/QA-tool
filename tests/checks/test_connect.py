import pytest

from qa_tool.checks import load_all
from qa_tool.checks.core import connect
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.checks import CREDENTIALS_REJECTED, REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.probe import RtspProbe
from tests.mock_camera import start_fleet

load_all()
SPEC = REGISTRY.get("CORE-CONNECT-1")


@pytest.fixture
def mock():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def ctx_for(mock, password="admin"):
    camera = Camera(
        slot=1, address="127.0.0.1", port=mock.port, rtsp_port=mock.rtsp_port, username="admin"
    )
    return CheckContext(
        camera=camera, password=password, place=Place.COLOR_BOX, extra={"timeout_s": 1}
    )


def test_live_camera_passes_with_passport(mock):
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason
    passport = result.data[PASSPORT_KEY]
    assert (passport["model"], passport["serial"], passport["firmware"]) == (
        "MOCK-1",
        "SN0001",
        "0.1.0",
    )
    assert result.evidence and "***</wsse:Password>" in result.evidence[0].content


def test_switched_off_camera_fails_with_reason(mock):
    ctx = ctx_for(mock)
    mock.stop()
    result = run_check(SPEC, ctx)
    assert result.status is Status.FAILED
    assert f"нет связи по HTTP (порт {ctx.camera.port})" in result.reason


def test_wrong_password_is_stand_error_not_camera_defect(mock):
    result = run_check(SPEC, ctx_for(mock, password="bad"))
    assert result.status is Status.STAND_ERROR
    assert "не приняла логин или пароль места 1" in result.reason


@pytest.mark.parametrize("reply", ["onvif", "gsoap", "http401"])
def test_rejected_password_is_stand_error_for_every_kind_of_refusal(mock, reply):
    mock.auth_reply = reply
    result = run_check(SPEC, ctx_for(mock, password="wrong"))
    assert result.status is Status.STAND_ERROR, result.reason
    assert result.data[CREDENTIALS_REJECTED]


def test_rtsp_closing_silently_is_written_down_not_hidden(mock, monkeypatch):
    monkeypatch.setattr(connect, "rtsp_probe", lambda *_: RtspProbe.CLOSED)
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason
    assert "закрыл его без ответа" in result.actual


def test_rtsp_not_answering_fails(mock, monkeypatch):
    monkeypatch.setattr(connect, "rtsp_probe", lambda *_: RtspProbe.NO_ANSWER)
    assert run_check(SPEC, ctx_for(mock)).status is Status.FAILED
