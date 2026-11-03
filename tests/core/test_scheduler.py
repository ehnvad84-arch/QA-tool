import json

import pytest

from qa_tool.core.models import Camera, Place, Scenario, Stand
from qa_tool.core.scheduler import Runner
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
        if event.kind == "result":
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
