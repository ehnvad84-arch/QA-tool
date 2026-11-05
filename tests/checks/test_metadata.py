from xml.etree import ElementTree

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import metadata
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.rtsp import RtpPacket
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
IDS = (metadata.CHECK_METADATA, metadata.CHECK_STREAM, metadata.CHECK_ANALYTICS)
needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


@pytest.fixture
def camera(request):
    cam = MockCamera(
        Passport("X", "M", "1", "S1"), state=OnvifState(**getattr(request, "param", {}))
    )
    cam.start()
    yield cam
    cam.stop()


def run(cam, check_id, wait=2.0):
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX)
    ctx.thresholds[metadata.WAIT_KEY] = Threshold(wait, "с", "тест")
    return run_check(REGISTRY.get(check_id), ctx)


@needs_video
def test_three_m_checks_pass(camera):
    for check_id in IDS:
        result = run(camera, check_id)
        assert result.status is Status.PASSED, f"{check_id}: {result.reason}"
    stream = run(camera, metadata.CHECK_STREAM)
    assert "MetadataStream: " in stream.actual and stream.threshold.value == 2.0
    names = {e.name for e in stream.evidence}
    assert {"sdp.txt", "metadata-1.xml"} <= names
    assert not any(
        r.startswith(("SET_PARAMETER", "ANNOUNCE", "RECORD")) for r in camera.rtsp.requests
    )


@pytest.mark.parametrize("camera", [{"profiles": ("S", "G")}], indirect=True)
def test_without_m_skipped(camera):
    for check_id in IDS:
        result = run(camera, check_id)
        assert result.status is Status.SKIPPED and result.reason == metadata.NOT_DECLARED


@needs_video
@pytest.mark.parametrize("camera", [{"metadata_stream": False}], indirect=True)
def test_m_declared_but_no_metadata_track(camera):
    result = run(camera, metadata.CHECK_STREAM)
    assert result.status is Status.FAILED and "нет дорожки vnd.onvif.metadata" in result.reason
    assert "video H264" in result.actual


@pytest.mark.parametrize(
    "camera", [{"analytics_modules": ("tt:CellMotionEngine", "acme:FaceCounter")}], indirect=True
)
def test_configured_module_not_supported(camera):
    result = run(camera, metadata.CHECK_ANALYTICS)
    assert result.status is Status.FAILED and "FaceCounter" in result.reason


def test_camera_gone_is_failed_not_passed(camera):
    camera.stop()
    for check_id in IDS:
        assert run(camera, check_id).status is Status.FAILED


def test_documents_assembled_by_marker():
    packets = [
        RtpPacket(0, False, b"<tt:MetadataStream xmlns:tt='http://www.onvif.org/ver10/schema'>"),
        RtpPacket(1, True, b"rtcp"),
        RtpPacket(0, True, b"</tt:MetadataStream>"),
        RtpPacket(0, True, b"<!DOCTYPE x [<!ENTITY a 'b'>]><MetadataStream>&a;</MetadataStream>"),
    ]
    docs = metadata.documents(packets, channel=0)
    assert len(docs) == 2
    assert metadata.is_metadata_stream(docs[0]) and not metadata.is_metadata_stream(docs[1])


def test_foreign_host_replaced_by_stand_address():
    fix = metadata.to_stand_address
    assert fix("rtsp://192.168.1.10/live", "10.0.0.5", 554) == "rtsp://10.0.0.5:554/live"
    assert fix("rtsp://192.168.1.10:8554/a?b=1", "10.0.0.5", 554) == "rtsp://10.0.0.5:8554/a?b=1"
    assert fix("rtsp://10.0.0.5/live", "10.0.0.5", 554) == "rtsp://10.0.0.5/live"


def test_profile_token_from_camera_is_escaped_in_request():
    class Soap:
        def call(self, _url, body):
            self.body = body
            return ElementTree.fromstring("<a><Uri>rtsp://x/</Uri></a>")

    soap = Soap()
    assert metadata.stream_uri(soap, "m2", "a&b<c>") == "rtsp://x/"
    assert "a&amp;b&lt;c&gt;" in soap.body and "a&b<c>" not in soap.body


def test_stand_port_applies_when_camera_names_no_port():
    uri = metadata.to_stand_address("rtsp://10.0.0.1/live", "10.0.0.1", 8554)
    assert uri == "rtsp://10.0.0.1:8554/live"


def test_stand_port_beats_the_port_camera_believes_in():
    uri = metadata.to_stand_address("rtsp://10.0.0.1:554/live", "10.0.0.1", 8554)
    assert uri == "rtsp://10.0.0.1:8554/live"


def test_impossible_port_from_camera_is_not_a_crash():
    uri = metadata.to_stand_address("rtsp://10.0.0.1:70000/live", "10.0.0.1", 554)
    assert uri == "rtsp://10.0.0.1:554/live"


def test_ipv6_stand_address_keeps_brackets():
    uri = metadata.to_stand_address("rtsp://[fd00::1]/live", "fd00::1", 8554)
    assert uri == "rtsp://[fd00::1]:8554/live"
