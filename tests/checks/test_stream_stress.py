"""STREAM-STRESS-1: долгая нагрузка на коротком сроке через ctx.extra."""

import json
import threading
import time

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.stream import stress
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Scenario, Threshold
from qa_tool.drivers.camera.rtsp import RtspClient
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState
from tests.mock_rtsp import StreamFault

load_all()
SPEC = REGISTRY.get(stress.CHECK_ID)
pytestmark = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)
FAST = {
    "stress_seconds": 3,
    "stream_silence_s": 0.6,
    "stream_come_back_s": 1.0,
    "stream_retry_s": 0.1,
    "stream_slice_s": 0.5,
}
MAIN = MockStream("main")
SUB = MockStream("sub", path="/live-sub")


def check(streams=(MAIN,), faults=None, extra=None, stop=lambda: False, thresholds=None, **server):
    result, rows, _ = run_with_camera(streams, faults, extra, stop, thresholds, **server)
    return result, rows


def run_with_camera(streams, faults, extra=None, stop=lambda: False, thresholds=None, **server):
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(streams=streams)).start()
    cam.rtsp.faults.update(faults or {})
    for key, value in server.items():
        setattr(cam.rtsp, key, value)
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    context = CheckContext(
        camera,
        "admin",
        Place.COLOR_BOX,
        extra={**FAST, **(extra or {})},
        stop_requested=stop,
        thresholds=thresholds or {},
    )
    try:
        result = run_check(SPEC, context)
    finally:
        cam.stop()
    return result, {row.check_id: row.result for row in result.children}, cam


def test_all_streams_hold_for_the_whole_term():
    result, rows = check((MAIN, SUB))
    assert result.status is Status.PASSED
    for row in rows.values():
        assert row.status is Status.PASSED, row.reason
    names = [e.name for e in rows["STREAM-STRESS-1"].evidence]
    assert "stress-1.csv" in names
    table = next(e.content for e in rows["STREAM-STRESS-1"].evidence if e.name == "stress-1.csv")
    assert table.startswith("минута,") and len(table.splitlines()) >= 4


def test_break_reconnects_and_fails_the_row():
    _, rows = check(faults={"/live": StreamFault(break_after_s=1)})
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "обрывов 1" in row.reason
    assert "пакетов" not in row.reason  # новая сессия — не потери


def test_camera_that_never_returns_ends_early_and_others_go_on():
    faults = {"/live": StreamFault(break_after_s=0.5, stays_down=True)}
    started = time.monotonic()
    _, rows = check((MAIN, SUB), faults, extra={"stress_seconds": 4})
    assert rows["STREAM-STRESS-1"].status is Status.FAILED
    assert "не вернулся" in rows["STREAM-STRESS-1"].reason
    assert rows["STREAM-STRESS-2"].status is Status.PASSED
    assert time.monotonic() - started >= 3.5  # второй поток дожил до конца срока


def test_silence_counts_as_a_break():
    _, rows = check(extra={"stress_seconds": 2}, silent=True)
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "обрывов" in row.reason


def test_stream_over_the_cameras_limit_fails_only_itself():
    third = MockStream("third", path="/live-third")
    _, rows = check((MAIN, SUB, third), max_plays=2)
    assert rows["STREAM-STRESS-1"].status is Status.PASSED
    assert rows["STREAM-STRESS-2"].status is Status.PASSED
    row = rows["STREAM-STRESS-3"]
    assert row.status is Status.FAILED
    assert "453" in row.reason and "2 уже шли" in row.reason


def test_growing_delay_fails():
    strict = {"stream.delay_growth_s": Threshold(0.2, "с", "тест")}
    _, rows = check(faults={"/live": StreamFault(lag=0.5)}, thresholds=strict)
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "задержка" in row.reason


def test_lost_packets_fail():
    _, rows = check(faults={"/live": StreamFault(lose_every=9)})
    assert "потеряла" in rows["STREAM-STRESS-1"].reason


def test_stop_ends_quickly_with_a_note():
    pressed = threading.Event()
    threading.Timer(1.2, pressed.set).start()
    started = time.monotonic()
    _, rows = check(extra={"stress_seconds": 60}, stop=pressed.is_set)
    assert time.monotonic() - started < 7
    assert "прервано оператором" in rows["STREAM-STRESS-1"].actual


def test_crash_in_a_stream_thread_leaves_a_row(monkeypatch):
    def crash(self):
        raise RuntimeError("сбой")

    monkeypatch.setattr(stress.StreamWatch, "_hold", crash)
    result, rows = check()
    assert result.status is Status.PASSED
    assert rows["STREAM-STRESS-1"].status is Status.STAND_ERROR


def test_parent_stays_passed_so_rows_survive_a_lost_camera():
    faults = {"/live": StreamFault(break_after_s=0.5, stays_down=True)}
    result, _ = check(faults=faults)
    assert result.status is Status.PASSED


def test_limit_includes_duration_and_room_to_finish():
    scenario = Scenario(stress_minutes=60)
    assert SPEC.stress
    assert SPEC.time_limit_s(scenario) > 3600 + stress.FINISH_S


def test_duration_comes_from_the_scenario():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(streams=(MAIN,))).start()
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    pressed = threading.Event()
    threading.Timer(1.0, pressed.set).start()
    context = CheckContext(
        camera, "admin", Place.COLOR_BOX, stress_minutes=30, stop_requested=pressed.is_set
    )
    try:
        result = run_check(SPEC, context)
    finally:
        cam.stop()
    assert "прервано оператором" in result.children[0].result.actual


def test_eight_cameras_at_once():
    results = {}

    def one(slot):
        results[slot] = check((MAIN, SUB), extra={"stress_seconds": 2})[1]

    threads = [threading.Thread(target=one, args=(slot,)) for slot in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    for rows in results.values():
        assert all(r.status is Status.PASSED for r in rows.values())


def summary(row):
    return json.loads(next(e.content for e in row.evidence if e.name == "stress-1.json"))


def test_answering_describe_is_not_coming_back():
    faults = {"/live": StreamFault(break_after_s=0.5, setup_refused_after_break=True)}
    _, rows, cam = run_with_camera((MAIN,), faults, {"stress_seconds": 4})
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "не вернулся" in row.reason
    assert "обрывов 1" in row.reason
    assert summary(row)["down_s"] >= 1.0
    assert cam.rtsp.requests.count("SETUP /live") < 15  # пауза между попытками, а не тысячи


def test_rtsp_alive_without_frames_is_downtime():
    faults = {"/live": StreamFault(break_after_s=0.5, no_frames_after_break=True)}
    _, rows = check(faults=faults, extra={"stress_seconds": 4})
    row = rows["STREAM-STRESS-1"]
    assert "не вернулся" in row.reason
    assert "обрывов 1" in row.reason
    assert summary(row)["down_s"] >= 1.0


def test_downtime_runs_from_last_packet_to_first_packet():
    _, rows = check(faults={"/live": StreamFault(break_after_s=0.5)})
    row = rows["STREAM-STRESS-1"]
    assert "обрывов 1" in row.reason
    assert 0.05 <= summary(row)["down_s"] < 1.0


def test_password_refused_after_break_is_not_repeated():
    faults = {"/live": StreamFault(break_after_s=0.5, rejects_after_break=True)}
    _, rows, cam = run_with_camera((MAIN, SUB), faults)
    assert rows["STREAM-STRESS-1"].status is Status.STAND_ERROR
    assert "логин и пароль" in rows["STREAM-STRESS-1"].reason
    assert cam.rtsp.password_tries == 1


def test_stop_before_first_frame_is_not_a_pass():
    moment = time.monotonic() + 0.3
    _, rows = check(stop=lambda: time.monotonic() > moment, silent=True)
    assert rows["STREAM-STRESS-1"].status is Status.SKIPPED


def test_stop_while_reconnecting_keeps_the_note():
    moment = time.monotonic() + 1.5
    faults = {"/live": StreamFault(break_after_s=0.5, setup_refused_after_break=True)}
    _, rows = check(
        faults=faults,
        extra={"stress_seconds": 10, "stream_come_back_s": 60},
        stop=lambda: time.monotonic() > moment,
    )
    assert "прервано оператором" in rows["STREAM-STRESS-1"].actual


def test_camera_may_assign_its_own_channel():
    _, rows = check(assigned_channel=2)
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.PASSED, row.reason
    assert summary(row)["frames"] > 0


def test_silent_stream_never_passes():
    _, rows = check(extra={"stress_seconds": 1, "stream_silence_s": 5}, silent=True)
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "видео не шло" in row.reason


def test_line_carries_delay_and_downtime():
    _, rows = check()
    assert "рост задержки" in rows["STREAM-STRESS-1"].actual
    assert "без потока" in rows["STREAM-STRESS-1"].actual


def test_onvif_exchange_reaches_the_first_row():
    _, rows = check()
    assert any(e.name.startswith("onvif-") for e in rows["STREAM-STRESS-1"].evidence)


@pytest.mark.parametrize(
    ("growth", "seconds", "failed"),
    [
        (3.5, 3600, False),  # часы: 29,97 кадра/с с метками на 30
        (0.18, 86_400, False),  # уход 50 ppm: худший час суток
        (30.0, 28_800, True),  # очередь в последний час восьми
        (2.0, 600, True),  # очередь: 12 с/ч
        (0.3, 60, False),  # быстро, но незаметно
    ],
)
def test_clock_drift_is_not_a_queue(growth, seconds, failed):
    context = CheckContext(Camera(1, "10.0.0.1", username="admin"), "", Place.COLOR_BOX)
    stats = {"delay_worst_s": growth, "delay_window_s": min(seconds, 3600)}
    assert bool(stress.delay_problem(context, stats)) is failed


def test_several_streams_breaking_at_once_send_the_password_once(monkeypatch):
    real = RtspClient.close

    def slow_close(self):
        time.sleep(0.02)  # закрытие не должно давать соседу окно для попытки с паролем
        real(self)

    monkeypatch.setattr(RtspClient, "close", slow_close)
    streams = tuple(MockStream(f"s{n}", path=f"/s{n}") for n in range(4))
    faults = {
        stream.path: StreamFault(break_after_s=0.5, rejects_after_break=True) for stream in streams
    }
    _, rows, cam = run_with_camera(streams, faults)
    assert cam.rtsp.password_tries == 1
    assert all(row.status is Status.STAND_ERROR for row in rows.values())


def test_refusal_on_play_sends_no_teardown():
    faults = {"/live": StreamFault(break_after_s=0.5, rejects_on_play_after_break=True)}
    _, rows, cam = run_with_camera((MAIN,), faults)
    assert rows["STREAM-STRESS-1"].status is Status.STAND_ERROR
    assert "TEARDOWN /live" not in cam.rtsp.requests[-3:]


def test_refusal_in_keepalive_stops_every_stream_of_the_camera():
    _, rows, cam = run_with_camera(
        (MAIN, SUB), None, {"stress_seconds": 6}, session_timeout=2, ping_refused=True
    )
    assert all(row.status is Status.STAND_ERROR for row in rows.values())
    assert cam.rtsp.requests.count("TEARDOWN /live") == 0
    assert cam.rtsp.password_tries <= 3


def test_rtcp_is_not_a_sign_of_life():
    faults = {"/live": StreamFault(rtcp_only_after_s=0.5)}
    _, rows = check(faults=faults, extra={"stress_seconds": 4})
    row = rows["STREAM-STRESS-1"]
    assert row.status is Status.FAILED
    assert "обрывов" in row.reason


def test_stop_at_first_open_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(stress, "start_video", lambda client: (_ for _ in ()).throw(TimeoutError()))
    _, rows = check(stop=lambda: True, extra={"stress_seconds": 1})
    assert rows["STREAM-STRESS-1"].status is Status.SKIPPED


def test_closing_time_is_not_counted_as_held(monkeypatch):
    real = RtspClient.close

    def slow_close(self):
        time.sleep(1.5)
        real(self)

    monkeypatch.setattr(RtspClient, "close", slow_close)
    moment = time.monotonic() + 1.2
    _, rows = check(extra={"stress_seconds": 10}, stop=lambda: time.monotonic() > moment)
    assert "видео шло" not in rows["STREAM-STRESS-1"].reason


def test_late_first_frame_is_not_short_video(monkeypatch):
    real = RtspClient.watch

    def late(self, on_packet, seconds, stop=lambda: False, silence_s=None):
        time.sleep(1.0)  # первый кадр пришёл позже PLAY
        return real(self, on_packet, seconds - 1.0, stop, silence_s)

    monkeypatch.setattr(RtspClient, "watch", late)
    moment = time.monotonic() + 2.4
    _, rows = check(extra={"stress_seconds": 10}, stop=lambda: time.monotonic() > moment)
    assert "видео шло" not in rows["STREAM-STRESS-1"].reason
