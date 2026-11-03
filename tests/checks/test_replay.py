import datetime

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import replay
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.tools import tool_path
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(replay.CHECK_ID)
needs_ffprobe = pytest.mark.skipif(tool_path("ffprobe") is None, reason="нет ffprobe")


def start(**state):
    return MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(**state)).start()


def ctx_for(cam, writes=False):
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={"replay_record_s": 0})
    ctx.allow_writes = writes
    return ctx


def one_recording():
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    return [{"token": "rec-1", "start": now - datetime.timedelta(minutes=5), "end": now}]


@needs_ffprobe
def test_archive_found_and_played():
    cam = start(recordings=one_recording())
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
        assert "h264" in result.actual and result.threshold.value == 10
        assert any(e.name == "replay.json" for e in result.evidence)
        assert "password" not in result.actual
    finally:
        cam.stop()


def test_no_records_without_writes_is_skipped():
    cam = start()
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.SKIPPED and "не разрешено сценарием" in result.reason
        assert cam.state.jobs[0]["mode"] == "Idle"
    finally:
        cam.stop()


@needs_ffprobe
def test_no_records_with_writes_records_plays_and_restores_job():
    cam = start()
    try:
        result = run_check(SPEC, ctx_for(cam, writes=True))
        assert result.status is Status.PASSED, result.reason
        assert cam.state.recordings and cam.state.jobs[0]["mode"] == "Idle"  # режим вернулся
    finally:
        cam.stop()


def test_replay_uri_refused_is_camera_defect():
    cam = start(recordings=one_recording(), replay_fault=True)
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.FAILED and result.reason.startswith("ONVIF:")
        assert SPEC.severity.value == "блокер"
    finally:
        cam.stop()


def test_camera_without_profile_g_is_skipped():
    cam = start(profiles=("S",))
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.SKIPPED and "Profile G" in result.reason
    finally:
        cam.stop()
