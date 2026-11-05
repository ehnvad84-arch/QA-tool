"""STREAM-STRESS-1: долгая нагрузка на коротком сроке через ctx.extra."""

import threading
import time

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.stream import stress
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Scenario, Threshold
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
    return result, {row.check_id: row.result for row in result.children}


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
