import datetime

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import replay
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.rtsp import RtpPacket, Track
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(replay.CHECK_ID)
needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


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


@needs_video
def test_archive_found_and_played():
    cam = start(recordings=one_recording())
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
        assert "h264" in result.actual and result.threshold.value == 10
        assert any(e.name == "replay.json" for e in result.evidence)
        assert "время кадров архива" in result.actual  # расширение RTP ONVIF разобрано
        replays = [h for m, path, h in cam.rtsp.headers if path.startswith("/replay")]
        assert replays and all(h.get("require") == "onvif-replay" for h in replays)
        play = next(h for m, _, h in cam.rtsp.headers if m == "PLAY")
        assert play["range"].startswith("clock=")  # архиву — абсолютное время (ONVIF)
        rtsp_log = next(e.content for e in result.evidence if e.name == "rtsp.txt")
        assert "admin" not in rtsp_log and "PLAY" in rtsp_log
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


@needs_video
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


@needs_video
def test_camera_without_require_support_gets_retry_without_header():
    cam = start(recordings=one_recording(), replay_require_unsupported=True)
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
        codes = [h.get("require") for m, _, h in cam.rtsp.headers if m == "DESCRIBE"]
        assert codes == ["onvif-replay", None]
    finally:
        cam.stop()


@needs_video
def test_foreign_replay_host_is_replaced_by_stand_address():
    cam = start(recordings=one_recording(), replay_host="192.0.2.77")  # адрес из TEST-NET
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason  # пошли на 127.0.0.1, а не на чужой
    finally:
        cam.stop()


@pytest.mark.parametrize(
    ("encoding", "payload", "key"),
    [
        ("H264", bytes([0x65, 0]), True),  # одиночный IDR
        ("H264", bytes([0x41, 0]), False),  # P-кадр
        ("H264", bytes([0x7C, 0x85]), True),  # FU-A, начало IDR
        ("H264", bytes([0x7C, 0x05]), False),  # FU-A, продолжение IDR
        ("H264", bytes([0x78, 0, 2, 0x67]), False),  # STAP-A с SPS
        ("H265", bytes([0x26, 0x01]), True),  # IDR_W_RADL (19)
        ("H265", bytes([0x02, 0x01]), False),  # TRAIL_R
        ("H265", bytes([0x62, 0x01, 0x93]), True),  # FU, начало IDR
        ("JPEG", b"\x00", True),
        ("MP4V-ES", b"\x00", False),
    ],
)
def test_keyframe_detection(encoding, payload, key):
    assert replay.is_keyframe(encoding, payload) is key


def test_summary_counts_by_rtp_clock_and_onvif_extension():
    moment = datetime.datetime(2026, 11, 4, 9, 0, tzinfo=datetime.UTC)
    ext = mock_rtsp.onvif_extension(moment, clean=True)
    packets = [
        RtpPacket(0, True, bytes([0x41]), timestamp=(2**32 - 45_000 + i * 9_000) % 2**32)
        for i in range(11)
    ]  # 1 с по 90 кГц, метки переходят через 2^32
    packets[0] = RtpPacket(0, True, bytes([0x41]), timestamp=packets[0].timestamp, extension=ext)
    packets.append(RtpPacket(1, False, b"", timestamp=0))  # RTCP другого канала не считается
    stats = replay.summarize(packets, Track("video", "H264", "x"), 0)
    assert stats["seconds"] == 1.0 and stats["frames"] == 11 and stats["keyframes"] == 1
    assert stats["archive"].startswith("2026-11-04 09:00:00")


def test_start_point_is_near_recording_end():
    rec = {"start": "2026-11-04T08:00:00Z", "end": "2026-11-04T09:00:00Z"}
    assert replay.start_point(rec, 10) == datetime.datetime(
        2026, 11, 4, 8, 59, 40, tzinfo=datetime.UTC
    )
    short = {"start": "2026-11-04T08:59:55Z", "end": "2026-11-04T09:00:00Z"}
    assert replay.start_point(short, 10).second == 55  # не раньше начала записи


def test_job_left_active_is_visible_as_changed_camera():
    cam = start(job_stuck_active=True)
    try:
        result = run_check(SPEC, ctx_for(cam, writes=True))
        assert result.status is Status.FAILED
        assert "камера осталась изменённой" in result.reason and "Active" in result.reason
    finally:
        cam.stop()


@needs_video
def test_frames_outside_recording_fail():
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    old = [
        {
            "token": "rec-1",
            "start": now - datetime.timedelta(hours=3),
            "end": now - datetime.timedelta(hours=2),
        }
    ]
    cam = start(recordings=old)  # mock отдаёт кадры «минуту назад» — не из этой записи
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.FAILED and "вне записи" in result.reason
    finally:
        cam.stop()


def test_outside_recording_without_onvif_time_is_reported():
    stats = {"seconds": 12.0, "frames": 120, "keyframes": 2, "codec": "h264"}
    rec = {"start": "2026-11-04T08:00:00Z", "end": "2026-11-04T09:00:00Z"}
    assert "нет времени кадров" in replay.outside_recording(stats, rec)


def test_record_time_above_limit_is_capped(monkeypatch):
    # большее время планировщик снял бы вместе с процессом — до возврата режимов записи
    seen = {}

    def record(_soap, _url, seconds):
        seen["seconds"] = seconds
        return []

    monkeypatch.setattr(replay, "record_for_a_while", record)
    cam = start()
    try:
        ctx = ctx_for(cam, writes=True)
        ctx.extra["replay_record_s"] = replay.DEFAULT_RECORD_S * 10
        run_check(SPEC, ctx)
        assert seen["seconds"] == replay.DEFAULT_RECORD_S
    finally:
        cam.stop()


def test_services_on_foreign_host_are_named_in_skip_reason():
    cam = start(foreign_services=("search",))
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.SKIPPED
        assert "на чужом хосте" in result.reason and "search" in result.reason
    finally:
        cam.stop()
