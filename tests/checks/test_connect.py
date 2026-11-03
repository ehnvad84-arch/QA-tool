import pytest

from qa_tool.checks import load_all
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
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


def test_wrong_password_fails_with_onvif_reason(mock):
    result = run_check(SPEC, ctx_for(mock, password="bad"))
    assert result.status is Status.FAILED
    assert "NotAuthorized" in result.reason
