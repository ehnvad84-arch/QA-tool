import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import stream
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import SoapClient
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(stream.CHECK_ID)
needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


def ctx_for(cam, password="admin"):
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    return CheckContext(camera, password, Place.COLOR_BOX)


@needs_video
def test_live_stream_plays_with_keyframes_and_no_password_in_evidence():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState()).start()
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
        assert "h264" in result.actual and result.threshold.value == 5
        log = next(e.content for e in result.evidence if e.name == "rtsp.txt")
        assert "PLAY" in log and "admin" not in log
        assert SPEC.severity.value == "блокер" and SPEC.help_id == "onvif-stream"
    finally:
        cam.stop()


def test_camera_gone_is_failed():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState()).start()
    ctx = ctx_for(cam)
    cam.stop()
    result = run_check(SPEC, ctx)
    assert result.status is Status.FAILED and result.reason.startswith("ONVIF:")


def test_profile_token_from_camera_is_escaped():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(profile_token="a&b")).start()
    try:
        camera = ctx_for(cam).camera
        soap = SoapClient("admin", "admin", timeout=5)
        profile, uri, source = stream.first_stream(soap, service_urls(soap, camera))
        assert profile == "a&b" and uri.startswith("rtsp://") and source == "vs-1"
    finally:
        cam.stop()
