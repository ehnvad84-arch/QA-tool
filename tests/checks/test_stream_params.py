"""STREAM-PARAMS-1: сверка параметров, потери, декодер."""

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.stream import common, params
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
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
