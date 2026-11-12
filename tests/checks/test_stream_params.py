"""STREAM-PARAMS-1: сверка параметров, потери, декодер."""

import subprocess

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.stream import common, params
from qa_tool.core.checks import REGISTRY, CheckContext, Severity, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.depacket import START_CODE
from qa_tool.drivers.camera.onvif_generic import EncoderSettings
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspError, Track
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState
from tests.mock_rtsp import StreamFault

load_all()
SPEC = REGISTRY.get(params.CHECK_ID)
pytestmark = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)
GOOD = MockStream("main")


@pytest.fixture(autouse=True)
def sample(monkeypatch):
    monkeypatch.setenv("QA_TOOL_STREAM_SAMPLE_S", "2.5")


def check(state, faults=None, stop=lambda: False):
    """Прогнать проверку на mock; строки — словарём по ID."""
    cam = MockCamera(Passport("X", "M", "1", "S1"), password="s3cret-pw", state=state).start()
    cam.rtsp.faults.update(faults or {})
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    try:
        result = run_check(
            SPEC, CheckContext(camera, "s3cret-pw", Place.COLOR_BOX, stop_requested=stop)
        )
    finally:
        cam.stop()
    return result, {row.check_id: row.result for row in result.children}


@pytest.fixture
def clean():
    return check(OnvifState(streams=(GOOD,)))


def test_clean_camera_passes_every_row(clean):
    result, rows = clean
    assert result.status is Status.PASSED
    assert set(rows) == {"STREAM-PARAMS-1", "STREAM-LOSS-1", "STREAM-DECODE-1"}
    assert all(r.status is Status.PASSED for r in rows.values()), rows
    actual = rows["STREAM-PARAMS-1"].actual
    assert "320×240" in actual and "10 кадр/с" in actual and "GOP 10" in actual
    assert "H264" in rows["STREAM-PARAMS-1"].expected


def test_password_is_in_no_evidence(clean):
    result, _ = clean
    everything = [*result.evidence, *(e for r in result.children for e in r.result.evidence)]
    assert everything
    assert not any("s3cret-pw" in str(e.content) for e in everything)


@pytest.mark.parametrize(
    ("stream", "word"),
    [
        (MockStream("main", fps=25), "частота"),
        (MockStream("main", encoding="H265"), "кодек"),
        (MockStream("main", width=1920, height=1080), "разрешение"),
        (MockStream("main", gop=50), "GOP"),
        (MockStream("main", kbps=1), "выше"),
        (MockStream("main", kbps=50_000, constant=True), "ниже"),
    ],
)
def test_declared_and_measured_differ(stream, word):
    _, rows = check(OnvifState(streams=(stream,)))
    row = rows["STREAM-PARAMS-1"]
    assert row.status is Status.FAILED
    assert word in row.reason


def test_variable_bitrate_far_below_ceiling_is_fine():
    _, rows = check(OnvifState(streams=(MockStream("main", kbps=50_000),)))
    assert rows["STREAM-PARAMS-1"].status is Status.PASSED


def test_no_settings_anywhere_still_shows_what_was_measured():
    _, rows = check(OnvifState(streams=(GOOD,), media1_empty=True, media2_empty=True))
    row = rows["STREAM-PARAMS-1"]
    assert row.status is Status.SKIPPED
    assert "настройки" in row.reason
    assert "320×240" in row.actual


def test_lost_packets_over_tcp_are_the_cameras_fault():
    _, rows = check(OnvifState(streams=(GOOD,)), {"/live": StreamFault(lose_every=7)})
    row = rows["STREAM-LOSS-1"]
    assert row.status is Status.FAILED
    assert "потеряла" in row.reason


def test_skipped_frames_fail_the_loss_row():
    _, rows = check(OnvifState(streams=(GOOD,)), {"/live": StreamFault(drop_every=4)})
    row = rows["STREAM-LOSS-1"]
    assert row.status is Status.FAILED
    assert "пропущено" in row.reason


def test_broken_stream_is_a_failed_row():
    _, rows = check(OnvifState(streams=(GOOD,)), {"/live": StreamFault(break_after_s=0.5)})
    row = rows["STREAM-PARAMS-1"]
    assert row.status is Status.FAILED
    assert "прервался" in row.reason


def test_one_broken_profile_does_not_stop_the_others():
    sub = MockStream("sub", path="/live-sub")
    _, rows = check(OnvifState(streams=(GOOD, sub), uri_fault_for="main"))
    assert rows["STREAM-PARAMS-1"].status is Status.FAILED
    assert "GetStreamUri" in rows["STREAM-PARAMS-1"].reason
    assert rows["STREAM-PARAMS-2"].status is Status.PASSED


def test_profiles_over_the_limit_are_skipped(monkeypatch):
    monkeypatch.setenv("QA_TOOL_STREAM_SAMPLE_S", "0.2")
    streams = tuple(MockStream(f"p{n}", path=f"/live-{n}") for n in range(12))
    _, rows = check(OnvifState(streams=streams))
    assert rows["STREAM-PARAMS-9"].status is Status.SKIPPED
    assert "больше 8" in rows["STREAM-PARAMS-12"].reason
    assert "STREAM-LOSS-8" in rows and "STREAM-LOSS-9" not in rows


def test_scheduler_limit_covers_the_worst_case():
    worst = common.MAX_PROFILES * (common.SAMPLE_CEILING_S + common.OPEN_RESERVE_S)
    assert worst < SPEC.timeout_s


def test_no_ffmpeg_is_a_stand_error(monkeypatch):
    monkeypatch.setattr(common, "tool_path", lambda name: None)
    _, rows = check(OnvifState(streams=(GOOD,)))
    assert rows["STREAM-DECODE-1"].status is Status.STAND_ERROR


def test_decoder_errors_fail_the_decode_row(monkeypatch):
    monkeypatch.setattr(params, "decode", lambda path, encoding: (["bad slice"], {}))
    _, rows = check(OnvifState(streams=(GOOD,)))
    row = rows["STREAM-DECODE-1"]
    assert row.status is Status.FAILED
    assert "bad slice" in row.reason


def test_decoder_gets_neither_address_nor_password(monkeypatch):
    seen = []
    real = common._run
    monkeypatch.setattr(common, "_run", lambda command: seen.append(command) or real(command))
    check(OnvifState(streams=(GOOD,)))
    assert seen
    arguments = " ".join(" ".join(command) for command in seen)
    assert "rtsp://" not in arguments and "s3cret-pw" not in arguments


def test_stop_ends_the_check_without_a_verdict():
    _, rows = check(OnvifState(streams=(GOOD,)), stop=lambda: True)
    assert rows["STREAM-PARAMS-1"].status is Status.SKIPPED


def test_wrong_password_is_a_stand_error(monkeypatch):
    def refuse(client):
        raise RtspError("DESCRIBE → 401", 401)

    monkeypatch.setattr(params, "start_video", refuse)
    _, rows = check(OnvifState(streams=(GOOD,)))
    assert rows["STREAM-PARAMS-1"].status is Status.STAND_ERROR


def key(sequence, timestamp):
    return RtpPacket(0, True, b"\x65key", timestamp, b"", sequence, 0.0)


def other(sequence, timestamp):
    return RtpPacket(0, True, b"\x41not", timestamp, b"", sequence, 0.0)


def test_sampler_starts_at_a_keyframe_and_keeps_whole_frames(tmp_path):
    path = tmp_path / "sample"
    sampler = common.Sampler(path, Track("video", "H264", ""))
    sampler.add(other(1, 100))  # камера начала посреди группы кадров
    sampler.add(key(2, 200))
    sampler.add(other(3, 300))
    sampler.add(other(4, 400))  # последний кадр мог оборваться: не пишется
    assert sampler.close()
    data = path.read_bytes()
    assert data.count(b"\x00\x00\x00\x01") == 2
    assert data.startswith(b"\x00\x00\x00\x01\x65key")


def test_sampler_without_keyframe_has_nothing(tmp_path):
    sampler = common.Sampler(tmp_path / "sample", Track("video", "H264", ""))
    sampler.add(other(1, 100))
    sampler.add(other(2, 200))
    assert not sampler.close()


def test_sample_stops_whole_once_full(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "SAMPLE_LIMIT", 250)
    sampler = common.Sampler(tmp_path / "sample", Track("video", "H264", "x"))
    sampler.add(key(1, 0))
    sampler.add(other(2, 3600))
    sampler.add(RtpPacket(0, True, b"\x65" + b"\x00" * 500, 7200, b"", 3, 0.0))  # не влезает
    sampler.add(other(4, 10_800))  # влез бы, но без пропущенного кадра декодеру нечего ссылаться
    sampler.add(other(5, 14_400))
    sampler.close()
    written = (tmp_path / "sample").read_bytes()
    assert b"\x65" + b"\x00" * 500 not in written
    assert written.count(START_CODE) == 2


def test_endless_frame_does_not_grow_without_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "SAMPLE_LIMIT", 250)
    sampler = common.Sampler(tmp_path / "sample", Track("video", "H264", "x"))
    for n in range(100):
        sampler.add(RtpPacket(0, False, b"\x65" + b"\x00" * 99, 0, b"", n, 0.0))
    assert len(sampler._frame) < 1000
    sampler.close()


def test_decoder_out_of_time_is_the_stands_fault(monkeypatch):
    def too_long(path, encoding):
        raise subprocess.TimeoutExpired("ffmpeg", 20)

    monkeypatch.setattr(params, "decode", too_long)
    _, rows = check(OnvifState(streams=(GOOD,)))
    assert rows["STREAM-DECODE-1"].status is Status.STAND_ERROR


def test_decoder_crash_is_the_stands_fault(monkeypatch):
    monkeypatch.setattr(
        common,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 137, "", ""),
    )
    _, rows = check(OnvifState(streams=(GOOD,)))
    assert rows["STREAM-DECODE-1"].status is Status.STAND_ERROR
    assert "137" in rows["STREAM-DECODE-1"].reason


def test_crash_in_one_profile_keeps_the_other_rows(monkeypatch):
    real = params.ProfileRun.run

    def falls_once(self, seconds):
        if self.number == 1:
            raise IndexError("данные камеры")
        return real(self, seconds)

    monkeypatch.setattr(params.ProfileRun, "run", falls_once)
    state = OnvifState(streams=(MockStream("main"), MockStream("sub", path="/live-sub")))
    _, rows = check(state)
    assert rows["STREAM-PARAMS-1"].status is Status.STAND_ERROR
    assert rows["STREAM-PARAMS-2"].status is Status.PASSED


def encoder(**values):
    return EncoderSettings(**{"encoding": "H264", **values})


STATS = {"fps": 25.0, "arrival_fps": 0.0, "kbps": 1000.0, "gop": 25, "time_jumps": 0}
CONTEXT = CheckContext(Camera(1, "10.0.0.1", username="admin"), "", Place.COLOR_BOX)


def test_timestamps_disagreeing_with_arrival_fail():
    problems = params.param_problems(
        CONTEXT, encoder(fps=25), {**STATS, "arrival_fps": 22.0}, "H264", None
    )
    assert "кадры приходят" in problems[0]


def test_arrival_rate_within_limit_is_fine():
    stats = {**STATS, "arrival_fps": 24.2}
    assert params.param_problems(CONTEXT, encoder(fps=25), stats, "H264", None) == []


def test_jpeg_size_rounds_up_to_blocks_of_eight():
    settings = encoder(encoding="JPEG", width=960, height=540)
    assert params.param_problems(CONTEXT, settings, STATS, "JPEG", (960, 544)) == []
    assert params.param_problems(CONTEXT, settings, STATS, "JPEG", (640, 480))


def check_with_server(state, **server):
    cam = MockCamera(Passport("X", "M", "1", "S1"), password="s3cret-pw", state=state).start()
    for key, value in server.items():
        setattr(cam.rtsp, key, value)
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    try:
        result = run_check(SPEC, CheckContext(camera, "s3cret-pw", Place.COLOR_BOX))
    finally:
        cam.stop()
    return {row.check_id: row for row in result.children}


def test_basic_login_is_a_security_remark_and_nothing_more():
    rows = check_with_server(OnvifState(streams=(GOOD,)), basic_auth=True)
    assert rows["STREAM-AUTH-1"].result.status is Status.FAILED
    assert rows["STREAM-AUTH-1"].severity is Severity.MINOR
    assert "Basic" in rows["STREAM-AUTH-1"].result.reason
    assert rows["STREAM-PARAMS-1"].result.status is Status.PASSED


def test_camera_without_login_has_no_remark():
    assert "STREAM-AUTH-1" not in check_with_server(OnvifState(streams=(GOOD,)))


def test_digest_is_taken_when_camera_offers_both():
    assert "STREAM-AUTH-1" not in check_with_server(OnvifState(streams=(GOOD,)), both_auth=True)


def test_basic_remark_survives_a_crash_in_the_profile(monkeypatch):
    def falls_after_login(self, seconds):
        self.basic_used = True
        raise IndexError("данные камеры")

    monkeypatch.setattr(params.ProfileRun, "run", falls_after_login)
    assert "STREAM-AUTH-1" in check_with_server(OnvifState(streams=(GOOD,)))
