"""STREAM-NET-1: потери и джиттер по UDP."""

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.stream import network
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState
from tests.mock_rtsp import StreamFault

load_all()
SPEC = REGISTRY.get(network.CHECK_ID)
pytestmark = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)
# порог мягче рабочего: на занятой машине тестов mock не держит ровный темп
CALM = {"stream.jitter_ms": Threshold(10_000.0, "мс", "тест")}


@pytest.fixture(autouse=True)
def sample(monkeypatch):
    monkeypatch.setenv("QA_TOOL_STREAM_SAMPLE_S", "1.5")


def check(state, faults=None, thresholds=None):
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=state).start()
    cam.rtsp.faults.update(faults or {})
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    context = CheckContext(camera, "admin", Place.COLOR_BOX, thresholds=thresholds or CALM)
    try:
        result = run_check(SPEC, context)
    finally:
        cam.stop()
    return {row.check_id: row.result for row in result.children}


def state(**options):
    return OnvifState(streams=(MockStream("main"),), **options)


def test_clean_udp_passes():
    row = check(state())["STREAM-NET-1"]
    assert row.status is Status.PASSED, row.reason
    assert "потеряно 0 %" in row.actual


def test_every_profile_gets_a_row():
    streams = (MockStream("main"), MockStream("sub", path="/live-sub"))
    rows = check(OnvifState(streams=streams))
    assert rows["STREAM-NET-1"].status is rows["STREAM-NET-2"].status is Status.PASSED


def test_loss_above_threshold_fails():
    row = check(state(), {"/live": StreamFault(lose_every=5)})["STREAM-NET-1"]
    assert row.status is Status.FAILED
    assert "потеряно" in row.reason


def test_jitter_above_threshold_fails():
    strict = {"stream.jitter_ms": Threshold(0.0, "мс", "тест")}
    row = check(state(), thresholds=strict)["STREAM-NET-1"]
    assert row.status is Status.FAILED
    assert "джиттер" in row.reason


def test_blocked_udp_is_not_the_cameras_fault(monkeypatch):
    monkeypatch.setattr(network, "UDP_SILENCE_S", 0.6)
    row = check(state(rtsp_udp_blackhole=True))["STREAM-NET-1"]
    assert row.status is Status.SKIPPED
    assert "не дошёл" in row.reason


def test_camera_refusing_udp_fails():
    row = check(state(rtsp_udp=False))["STREAM-NET-1"]
    assert row.status is Status.FAILED
    assert "461" in row.reason


def test_stream_cut_mid_sample_fails():
    row = check(state(), {"/live": StreamFault(break_after_s=0.5)})["STREAM-NET-1"]
    assert row.status is Status.FAILED
    assert "прервался" in row.reason


def test_profile_without_address_is_left_to_the_params_row():
    row = check(state(uri_fault_for="main"))["STREAM-NET-1"]
    assert row.status is Status.SKIPPED
