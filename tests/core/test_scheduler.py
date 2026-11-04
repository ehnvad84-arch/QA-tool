import json

import pytest

from qa_tool.core.models import Camera, Place, Scenario, Stand
from qa_tool.core.scheduler import Runner
from qa_tool.core.worker import LOST_DURING_CHECK
from tests.fake_checks import MAIN, SLOW
from tests.mock_camera import start_fleet

REF = "tests.fake_checks:REGISTRY"
PHASE = {"FAKE-NET-1": 1, "FAKE-NET-2": 1, "FAKE-SYNC-1": 2, "FAKE-LIGHT-1": 3, "FAKE-STRESS-1": 4}


@pytest.fixture
def fleet(monkeypatch):
    cameras, _ = start_fleet(8)
    for slot in range(1, 9):
        monkeypatch.setenv(f"QA_TOOL_PASSWORD_{slot}", "admin")
    yield cameras
    for cam in cameras:
        cam.stop()


def make_stand(tmp_path, fleet):
    cams = [
        Camera(slot=i, address="127.0.0.1", port=m.port, rtsp_port=m.rtsp_port, username="admin")
        for i, m in enumerate(fleet, start=1)
    ]
    return Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=cams)


def read_trace(trace_dir):
    rows = []
    for path in trace_dir.glob("*.jsonl"):
        slot = int(path.stem)
        for line in path.read_text(encoding="utf-8").splitlines():
            rows.append({"slot": slot, **json.loads(line)})
    return rows


def test_eight_cameras_phases_locks_and_barrier(tmp_path, fleet):
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    runner = Runner(
        make_stand(tmp_path, fleet),
        Scenario(checks=MAIN),
        registry_ref=REF,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()

    results = store.results()
    assert len(results) == 8 * len(MAIN)
    assert {r["status"] for r in results} == {"прошла"}, [
        dict(r) for r in results if r["status"] != "прошла"
    ]

    trace = read_trace(trace_dir)
    light = sorted(
        (t for t in trace if t["id"] in ("FAKE-LIGHT-1", "FAKE-SYNC-1")), key=lambda t: t["start"]
    )
    sequential = [t for t in light if t["id"] == "FAKE-LIGHT-1"]
    for a, b in zip(sequential, sequential[1:], strict=False):
        assert a["end"] <= b["start"], "проверки с общим ресурсом пересеклись"
    sync = [t for t in trace if t["id"] == "FAKE-SYNC-1"]
    assert len(sync) == 8
    assert max(t["start"] for t in sync) - min(t["start"] for t in sync) <= 1.0
    sync_end = max(t["end"] for t in sync)
    assert all(t["start"] >= sync_end for t in sequential), "свет занят синхронной проверкой"

    for phase in (1, 2, 3):
        ends = [t["end"] for t in trace if PHASE[t["id"]] == phase]
        starts = [t["start"] for t in trace if PHASE[t["id"]] == phase + 1]
        assert max(ends) <= min(starts), f"фазы {phase} и {phase + 1} перемешаны"
    assert store.run_info()["state"] == "завершён"


def test_stop_skips_unstarted_checks(tmp_path, fleet):
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    stand = make_stand(tmp_path, fleet[:2])
    runner = None

    def on_event(event):
        # остановка во время первой проверки: у координатора целая секунда, чтобы успеть
        if event.kind == "started":
            runner.stop()

    runner = Runner(
        stand,
        Scenario(checks=SLOW),
        registry_ref=REF,
        on_event=on_event,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()
    statuses = [(r["status"], r["reason"]) for r in store.results()]
    assert len(statuses) == 2 * len(SLOW)
    assert ("пропущена", "прогон остановлен") in statuses
    assert store.run_info()["state"] == "остановлен"


def test_lost_camera_marked_stand_error_others_finish(tmp_path, fleet):
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    def on_event(event):
        # сеть пропадает, пока идёт первая проверка камеры 3: она сама камеру не трогает
        # и проходит, а проверка связи перед второй уже не проходит
        if event.kind == "started" and event.slot == 3 and event.check_id == SLOW[0]:
            fleet[2].stop()

    runner = Runner(
        make_stand(tmp_path, fleet),
        Scenario(checks=SLOW),
        registry_ref=REF,
        on_event=on_event,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()
    for slot in range(1, 9):
        rows = store.results(slot=slot)
        assert len(rows) == len(SLOW)
        if slot == 3:
            assert rows[0]["status"] == "прошла"
            assert {(r["status"], r["reason"]) for r in rows[1:]} == {
                ("ошибка стенда", "нет связи")
            }
        else:
            assert {r["status"] for r in rows} == {"прошла"}, (
                slot,
                [(r["check_id"], r["status"], r["reason"]) for r in rows],
            )
    assert store.run_info()["state"] == "завершён"


def test_hung_check_removes_only_that_camera(tmp_path, fleet):
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    stand = make_stand(tmp_path, fleet[:2])
    runner = Runner(
        stand,
        Scenario(checks=["FAKE-HANG-1", "FAKE-NET-1"]),
        registry_ref=REF,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()
    for slot in (1, 2):
        rows = {r["check_id"]: (r["status"], r["reason"]) for r in store.results(slot=slot)}
        assert rows["FAKE-HANG-1"] == ("ошибка стенда", "проверка зависла (> 1 с)")
        assert rows["FAKE-NET-1"][0] == "ошибка стенда"


def test_camera_lost_during_check_is_camera_defect(tmp_path, fleet):
    # решение директора 2026-11-03: перестала отвечать во время проверки — сбой камеры
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    def on_event(event):
        if event.kind == "started" and event.slot == 3 and event.check_id == "FAKE-FAILS-1":
            fleet[2].stop()

    stand = make_stand(tmp_path, fleet[:4])
    runner = Runner(
        stand,
        Scenario(checks=["FAKE-SLOW-1", "FAKE-FAILS-1"]),
        registry_ref=REF,
        on_event=on_event,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()
    lost = {r["check_id"]: (r["status"], r["reason"]) for r in store.results(slot=3)}
    assert lost["FAKE-FAILS-1"] == ("не прошла", LOST_DURING_CHECK), lost
    alive = {r["check_id"]: r["status"] for r in store.results(slot=1)}
    assert alive["FAKE-FAILS-1"] == "не прошла"


def test_three_sync_checks_on_shared_light_with_vendor_filter_do_not_hang(tmp_path, fleet):
    import threading

    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    cams = [
        Camera(slot=1, address="127.0.0.1", port=fleet[0].port, username="admin", vendor="a"),
        Camera(slot=2, address="127.0.0.1", port=fleet[1].port, username="admin", vendor="b"),
    ]
    stand = Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=cams)
    runner = Runner(
        stand,
        Scenario(checks=["FAKE-SYNC-X", "FAKE-SYNC-Y", "FAKE-SYNC-Z"]),
        registry_ref=REF,
        extra={"trace_dir": str(trace_dir)},
    )
    done = {}

    def run():
        store = runner.run()
        done["rows"] = [dict(r) for r in store.results()]  # база привязана к потоку прогона

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=40)
    if worker.is_alive():
        runner.stop()
        worker.join(timeout=20)
        raise AssertionError("прогон завис на барьере синхронных проверок")
    statuses = {(r["slot"], r["check_id"]): r["status"] for r in done["rows"]}
    assert statuses[(2, "FAKE-SYNC-Y")] == "прошла"
    assert (1, "FAKE-SYNC-Y") not in statuses
    assert {statuses[(s, c)] for s in (1, 2) for c in ("FAKE-SYNC-X", "FAKE-SYNC-Z")} == {"прошла"}


def test_coordinator_freeze_extends_deadlines():
    import time
    from types import SimpleNamespace

    runner = Runner.__new__(Runner)
    cam = SimpleNamespace(alive=True, running=("FAKE-SLOW-1", 100.0))
    runner.cams = {1: cam}
    runner._compensate_freeze(time.monotonic() - 30)  # координатор «спал» 30 с
    assert cam.running[1] >= 129.0


def test_camera_lost_during_first_check_is_camera_defect(tmp_path, fleet):
    # первая проверка видит открытый порт, а к концу камера пропала — тоже сбой камеры
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    def on_event(event):
        if event.kind == "started" and event.slot == 2 and event.check_id == "FAKE-FAILS-1":
            fleet[1].stop()

    stand = make_stand(tmp_path, fleet[:2])
    runner = Runner(
        stand,
        Scenario(checks=["FAKE-FAILS-1"]),
        registry_ref=REF,
        on_event=on_event,
        extra={"trace_dir": str(trace_dir)},
    )
    store = runner.run()
    rows = {r["slot"]: (r["status"], r["reason"]) for r in store.results()}
    assert rows[2] == ("не прошла", LOST_DURING_CHECK)
    assert rows[1][0] == "не прошла"


def test_composite_check_writes_child_rows_and_tasks(tmp_path, fleet):
    from qa_tool.report.model import build_protocol

    events = []
    stand = make_stand(tmp_path, fleet[:1])
    runner = Runner(
        stand, Scenario(checks=["FAKE-COMPOSITE-1"]), registry_ref=REF, on_event=events.append
    )
    store = runner.run()
    rows = {r["check_id"]: (r["status"], r["reason"]) for r in store.results()}
    assert list(rows) == ["SUB-1", "SUB-2", "SUB-3"]
    assert rows["SUB-1"][0] == "прошла"
    assert rows["SUB-2"] == ("не прошла", "password=*** отказ")
    assert rows["SUB-3"][0] == "ошибка стенда"  # «прошла» без доказательства
    evidence = next((store.run_dir / "evidence" / "1" / "SUB-1").iterdir()).read_text()
    assert "s3cret" not in evidence
    protocol = build_protocol(store, 1)
    assert [t.title.split("]")[0] for t in protocol.tasks] == ["[SUB-2"]
    parts = [e for e in events if e.kind == "result"]
    assert [e.check_id for e in parts] == ["SUB-1", "SUB-2", "SUB-3"] and all(e.part for e in parts)
    assert [e.check_id for e in events if e.kind == "done"] == ["FAKE-COMPOSITE-1"]


def test_events_carry_passport_severity_and_clean_reasons(tmp_path, fleet):
    events = []
    stand = make_stand(tmp_path, fleet[:1])
    runner = Runner(
        stand,
        Scenario(checks=["FAKE-PASSPORT-1", "FAKE-COMPOSITE-1", "FAKE-NET-1"]),
        registry_ref=REF,
        on_event=events.append,
    )
    runner.run()
    kinds = [e.kind for e in events if e.slot == 1]
    assert kinds.index("passport") < kinds.index("result")
    passport = next(e for e in events if e.kind == "passport")
    assert passport.passport["model"] == "<b>FK-1</b>"  # разметку экранирует окно, не ядро
    results = {e.check_id: e for e in events if e.kind == "result"}
    assert results["SUB-2"].severity == "блокер"
    assert results["SUB-2"].detail == "password=*** отказ"
    assert results["SUB-1"].severity == results["FAKE-NET-1"].severity != ""


def test_wrong_password_skips_other_checks_instead_of_false_failures(tmp_path, fleet, monkeypatch):
    # решение директора 2026-11-03: неверный пароль — ошибка стенда, задач программистам нет
    from qa_tool.report.model import build_protocol

    monkeypatch.setenv("QA_TOOL_PASSWORD_1", "wrong")
    stand = make_stand(tmp_path, fleet[:2])
    runner = Runner(stand, Scenario(checks=["CORE-CONNECT-1", "RUV-S-STREAM-1"]))
    store = runner.run()
    bad = {r["check_id"]: (r["status"], r["reason"]) for r in store.results(slot=1)}
    assert bad["CORE-CONNECT-1"][0] == "ошибка стенда"
    assert bad["RUV-S-STREAM-1"] == (
        "пропущена",
        "камера не приняла логин или пароль (см. CORE-CONNECT-1)",
    )
    assert build_protocol(store, 1).tasks == []
    good = {r["check_id"]: r["status"] for r in store.results(slot=2)}
    assert good == {"CORE-CONNECT-1": "прошла", "RUV-S-STREAM-1": "прошла"}
