"""Прошлая прошивка той же модели (этап 8, T17): какой прогон берётся, график, враждебные папки."""

import json

from qa_tool.checks import load_all
from qa_tool.core.checks import REGISTRY, CheckResult, ChildResult, Evidence, Status
from qa_tool.core.models import Camera, Place, Scenario, Stand
from qa_tool.core.store import RunStore
from qa_tool.report.codec_history import NONE_FOUND, _inside, find
from qa_tool.report.html import render_protocol
from qa_tool.report.model import build_protocol

load_all()
SPEC = REGISTRY.get("CODEC-QUALITY-1")


def make_run(runs, firmware, vmafs, manufacturer="X", model="M-1"):
    """Прогон с одной строкой кодека: ступени `vmafs` (кбит/с → VMAF) и паспорт камеры."""
    camera = Camera(slot=1, address="127.0.0.1", port=80, rtsp_port=554, username="admin")
    stand = Stand(place=Place.COLOR_BOX, runs_dir=runs, cameras=[camera])
    store = RunStore.create(stand, Scenario(checks=[SPEC.id], allow_writes=True))
    passport = {"manufacturer": manufacturer, "model": model, "firmware": firmware}
    store.set_passport(1, passport)
    measure = {
        "версия": 1,
        "кодек": "H264",
        "разрешение": [1920, 1080],
        "частота": 25,
        "кодировщик": {"токен": "ve", "служба": "media2", "профили": [["p1", "Main"]]},
        "эталон": {"кодек": "H264", "битрейт": 8192, "почему": "тест"},
        "ступени": [
            {
                "задано": kbps,
                "в потоке": kbps,
                "качество": {"vmaf": [v, 0.5, v], "ssim": [0.95, 0, 0.95], "psnr": [40, 0, 40]},
            }
            for kbps, v in vmafs.items()
        ],
        "правило": {"codec.h264.fhd.vmaf_min": {"значение": 70.0, "источник": "тест"}},
    }
    row = CheckResult(
        Status.PASSED,
        reason="тест",
        evidence=[Evidence("measure.json", json.dumps(measure, ensure_ascii=False))],
    )
    result = CheckResult(
        Status.PASSED, children=[ChildResult("CODEC-H264-1", "Кодеки: H.264 — Main", row)]
    )
    store.add_result(1, SPEC, result, "t")
    return store


def test_previous_firmware_of_same_model_is_drawn_against_this_one(tmp_path):
    runs = tmp_path / "runs"
    old = make_run(runs, "1.3.9", {4096: 80.0, 2048: 70.0})
    make_run(runs, "1.4.0", {4096: 10.0}, model="ДРУГАЯ")  # другая модель — не берётся
    now = make_run(runs, "1.4.2", {4096: 90.0, 2048: 85.0}, manufacturer=" x ", model="m-1 ")
    protocol = build_protocol(now, 1)
    appendix = protocol.codec
    assert appendix is not None
    assert f"прошлая 1.3.9 (прогон {old.run_id})" in appendix.history
    sheet = appendix.sheets[0]
    assert sheet.history_chart and (now.run_dir / sheet.history_chart).exists()
    html = render_protocol(protocol)
    assert "4. Эта прошивка и прошлая" in html and sheet.history_chart in html


def test_same_firmware_or_later_run_is_not_a_previous_firmware(tmp_path):
    runs = tmp_path / "runs"
    first = make_run(runs, "1.4.2", {4096: 80.0, 2048: 70.0})
    second = make_run(runs, "1.4.2", {4096: 90.0, 2048: 85.0})
    later = make_run(runs, "2.0", {4096: 90.0, 2048: 85.0})
    assert later.run_id > second.run_id
    assert build_protocol(second, 1).codec.history == NONE_FOUND  # та же прошивка
    assert build_protocol(first, 1).codec.history == NONE_FOUND  # прогон 2.0 — позже


def test_unknown_model_is_not_searched(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "1.0", {4096: 80.0, 2048: 70.0}, model="")
    now = make_run(runs, "2.0", {4096: 80.0, 2048: 70.0}, model="")
    assert find(now.run_dir, {"manufacturer": "X", "model": "", "firmware": "2.0"}) is None


def test_evidence_folder_outside_its_run_is_ignored(tmp_path):
    run = tmp_path / "runs" / "QA-20261109-001"
    (run / "evidence").mkdir(parents=True)
    assert _inside(run, "evidence") is not None
    assert _inside(run, "../QA-20261109-002/evidence") is None
    assert _inside(run, "/etc") is None
    assert _inside(run, "") is None


def test_broken_old_database_is_skipped(tmp_path):
    runs = tmp_path / "runs"
    broken = runs / "QA-20000101-001"
    broken.mkdir(parents=True)
    (broken / "run.sqlite").write_bytes(b"not a database")
    now = make_run(runs, "2.0", {4096: 80.0, 2048: 70.0})
    assert build_protocol(now, 1).codec.history == NONE_FOUND
