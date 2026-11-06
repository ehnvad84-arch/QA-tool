import dataclasses
import datetime

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import replay
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.rtsp import RtpPacket, Track
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(replay.CHECK_ID)
needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


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
def test_recording_after_clock_reset_is_kept():
    begin = datetime.datetime(2026, 11, 6, 12, tzinfo=datetime.UTC)
    recording = {"start": begin.isoformat(), "end": EPOCH.isoformat()}
    assert replay.has_data(recording)  # конец раньше начала: часы сброшены, а данные есть


def test_search_passes_with_no_records_and_fails_on_refusal():
    search = REGISTRY.get(replay.SEARCH_CHECK_ID)
    cam = start()
    try:
        result = run_check(search, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
        assert "записей: 0" in result.actual
    finally:
        cam.stop()


def test_search_refused_by_camera_is_a_failure(monkeypatch):
    def refuse(*_):
        raise OnvifFault("FindRecordings не вернул SearchToken")

    monkeypatch.setattr(replay, "find_recordings", refuse)
    cam = start()
    try:
        assert run_check(REGISTRY.get(replay.SEARCH_CHECK_ID), ctx_for(cam)).status is Status.FAILED
    finally:
        cam.stop()


@needs_video
def test_silent_play_without_range_does_not_blame_seeking(monkeypatch):
    monkeypatch.setattr(replay, "start_point", lambda *_: None)
    cam = start(recordings=one_recording(), replay_range_silent=False)
    calls = []
    real = replay.play

    def silent(client, seconds, start_at):
        calls.append(start_at)
        _, stats = real(client, seconds, start_at)
        return "", {**stats, "frames": 0, "keyframes": 0, "seconds": 0.0}

    monkeypatch.setattr(replay, "play", silent)
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.FAILED
        assert "переход по времени" not in result.reason
        assert calls == [None]
    finally:
        cam.stop()


@pytest.mark.parametrize("moment", [None, EPOCH], ids=["сейчас", "1970"])
def test_empty_recording_of_camera_without_card_is_not_an_archive(moment):
    now = moment or datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    cam = start(recordings=[{"token": "rec-1", "start": now, "end": now}])
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.SKIPPED, result.reason
    finally:
        cam.stop()


@needs_video
def test_archive_playing_only_from_start_names_broken_seek():
    cam = start(recordings=one_recording(), replay_range_silent=True)
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.FAILED
        assert result.reason.startswith("переход по времени не работает")
        assert "кадров 0" not in result.actual  # в протоколе — то, что проиграно с начала
        plays = [h for m, _, h in cam.rtsp.headers if m == "PLAY"]
        assert "range" in plays[0] and "range" not in plays[1]
    finally:
        cam.stop()


def test_camera_closing_finished_search_itself_keeps_the_found_records():
    cam = start(recordings=one_recording(), end_search_fault=True)  # так делает Dahua
    try:
        result = run_check(SPEC, ctx_for(cam))
        assert result.status is Status.PASSED, result.reason
    finally:
        cam.stop()


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


def test_key_frame_split_into_packets_is_counted_once():
    track = Track("video", "H264", "c", 90000)
    key = bytes([0x7C, 0x85])  # признак ключевого на каждом пакете кадра: так шлёт Dahua
    packets = [
        RtpPacket(0, False, key, 1000),
        RtpPacket(0, True, key, 1000),
        RtpPacket(0, True, key, 4600),
    ]
    assert replay.summarize(packets, track, 0)["keyframes"] == 2


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


@needs_video
def test_camera_behind_tunnel_reaches_services_it_names_by_its_own_address():
    cam = start(recordings=one_recording(), foreign_services=("search", "replay"))
    try:
        ctx = ctx_for(cam)
        ctx.camera = dataclasses.replace(ctx.camera, lan_address="10.9.9.9")
        result = run_check(SPEC, ctx)
        assert result.status is Status.PASSED, result.reason
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


def test_refused_recording_with_modes_restored_is_a_plain_failure():
    cam = start(job_refuses_active="job-1")
    try:
        result = run_check(SPEC, ctx_for(cam, writes=True))
        assert result.status is Status.FAILED
        assert "камера осталась изменённой" not in result.reason
        assert cam.state.jobs[0]["mode"] == "Idle"
    finally:
        cam.stop()


def test_refused_recording_does_not_hide_a_job_left_active():
    cam = start(job_refuses_active="job-2", job_stuck_active=True)
    cam.state.jobs.append({"token": "job-2", "recording": "rec-2", "mode": "Idle"})
    try:
        result = run_check(SPEC, ctx_for(cam, writes=True))
        assert result.status is Status.FAILED
        assert "камера осталась изменённой: запись не включилась" in result.reason
        assert "задание job-1: осталось Active" in result.reason
    finally:
        cam.stop()


def test_lost_connection_while_enabling_recording_still_restores_modes(monkeypatch):
    calls = []

    def set_mode(_soap, _url, job, mode):
        calls.append(mode)
        if mode == "Active":
            raise CameraUnreachable("нет ответа")

    monkeypatch.setattr(replay, "_job_modes", lambda *_: [("job-1", "Idle")])
    monkeypatch.setattr(replay, "_set_mode", set_mode)
    with pytest.raises(CameraUnreachable):
        replay.record_for_a_while(None, "url", 0)
    assert calls == ["Active", "Idle"]
