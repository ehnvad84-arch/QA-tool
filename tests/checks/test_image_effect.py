"""IMAGE-EFFECT-1 на виртуальной камере (этап 7, T9): положения 0/50/100 %, итоги, возврат."""

import re

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.image import engine, frames
from qa_tool.checks.image.engine import CHECK_ID, RESTORE_ID, STATE_ID
from qa_tool.core.checks import REGISTRY, CheckContext, Phase, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import IMAGING_FULL, OnvifState

load_all()
SPEC = REGISTRY.get(CHECK_ID)
needs_video = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None or tool_path("ffprobe") is None, reason="нет ffmpeg"
)
MANUAL = IMAGING_FULL.replace(
    "<tt:Exposure><tt:Mode>AUTO</tt:Mode>", "<tt:Exposure><tt:Mode>MANUAL</tt:Mode>"
)
FAST = {
    "image.apply_delay_s": 0.0,
    "image.before_gap_s": 0.0,
    "image.clip_s": 0.6,
    "image.settle_frames": 3.0,
}


@pytest.fixture
def camera_with():
    started = []

    def start(xml=MANUAL, **state):
        cam = MockCamera(
            Passport("X", "M", "1", "S1"),
            state=OnvifState(imaging_xml=xml, imaging_video=True, **state),
        ).start()
        started.append(cam)
        return cam

    yield start
    for cam in started:
        cam.stop()


def run(cam, settings=None, allow_writes=True, **extra):
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    thresholds = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
    ctx = CheckContext(
        target, "admin", Place.COLOR_BOX, thresholds, allow_writes=allow_writes, extra=extra
    )
    if settings:
        ctx.extra[engine.SETTINGS_KEY] = settings
    return run_check(SPEC, ctx)


def rows(result):
    return {c.check_id: c.result for c in result.children}


def test_registration():
    assert SPEC.phase is Phase.SYNC and not SPEC.writes and SPEC.read_only_note
    assert SPEC.places == frozenset({Place.COLOR_BOX}) and SPEC.timeout_s >= 1700


@needs_video
def test_healthy_settings_pass_with_three_snapshots_and_are_restored(camera_with):
    cam = camera_with()
    before = cam.state.imaging_xml
    result = run(cam, ["brightness", "saturation", "wb_red"])
    found = rows(result)
    for check_id in ("IMAGE-BRIGHT-1", "IMAGE-SAT-1", "IMAGE-WB-1"):
        row = found[check_id]
        assert row.status is Status.PASSED, (check_id, row.reason)
        names = {e.name for e in row.evidence}
        assert {"p000.jpg", "p050.jpg", "p100.jpg", "before1.jpg", "after.jpg"} <= names
        assert "measure.json" in names and "уточнить" in row.threshold.source
    assert found[STATE_ID].status is Status.PASSED
    assert found[RESTORE_ID].status is Status.PASSED, found[RESTORE_ID].reason
    assert cam.state.imaging_xml == before  # камера ушла такой, какой пришла (AD-3)


@needs_video
def test_deaf_and_reversed_settings_fail(camera_with):
    cam = camera_with(imaging_effects={"saturation": "none", "contrast": "reverse"})
    found = rows(run(cam, ["saturation", "contrast"]))
    deaf, reversed_ = found["IMAGE-SAT-1"], found["IMAGE-CONTRAST-1"]
    assert deaf.status is Status.FAILED and "эффекта нет" in deaf.reason
    assert reversed_.status is Status.FAILED and reversed_.reason.startswith("эффект обратный")
    assert found[RESTORE_ID].status is Status.PASSED


@needs_video
def test_camera_that_does_not_take_a_value_fails_that_setting_only(camera_with):
    cam = camera_with(imaging_sticky={"Sharpness": "50"})
    found = rows(run(cam, ["sharpness", "saturation"]))
    assert found["IMAGE-SHARP-1"].status is Status.FAILED
    assert "не приняла значение" in found["IMAGE-SHARP-1"].reason
    assert found["IMAGE-SAT-1"].status is Status.PASSED


@needs_video
def test_all_nine_settings_give_rows_and_ordinary_scene_skips_wdr_and_blc(camera_with):
    cam = camera_with()
    progress = []
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    thresholds = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
    ctx = CheckContext(target, "admin", Place.COLOR_BOX, thresholds, allow_writes=True)
    ctx.progress = progress.append
    result = run_check(SPEC, ctx)
    found = rows(result)
    assert len(found) == 11, list(found)
    assert (
        found["IMAGE-WDR-1"].status is Status.SKIPPED
        and "сцена этапа 6" in found["IMAGE-WDR-1"].reason
    )
    assert found["IMAGE-BLC-1"].status is Status.SKIPPED
    for check_id in (
        "IMAGE-BRIGHT-1",
        "IMAGE-CONTRAST-1",
        "IMAGE-SHARP-1",
        "IMAGE-WB-2",
        "IMAGE-NR-1",
    ):
        assert found[check_id].status is Status.PASSED, (check_id, found[check_id].reason)
    assert found[RESTORE_ID].status is Status.PASSED
    assert any(p.startswith("Контраст · 50 % · 2 из 9") for p in progress), progress


# --- T10: без галочки и при автоэкспозиции ----------------------------------------------------


@needs_video
def test_without_checkbox_nothing_is_written_but_state_is_recorded(camera_with):
    cam = camera_with()
    result = run(cam, ["brightness", "saturation"], allow_writes=False)
    found = rows(result)
    assert cam.state.imaging_writes == []
    state = found[STATE_ID]
    assert state.status is Status.PASSED and {"state.jpg", "imaging.xml"} <= {
        e.name for e in state.evidence
    }
    assert "Яркость: 50" in next(e.content for e in state.evidence if e.name == "state.txt")
    for check_id in ("IMAGE-BRIGHT-1", "IMAGE-SAT-1"):
        assert found[check_id].status is Status.SKIPPED
        assert found[check_id].reason.startswith("не разрешено сценарием")
    assert found[RESTORE_ID].status is Status.SKIPPED


@needs_video
def test_dampened_effect_under_auto_exposure_is_skipped_with_explanation(camera_with):
    cam = camera_with(xml=IMAGING_FULL, imaging_effects={"brightness": "none"})
    row = rows(run(cam, ["brightness"]))["IMAGE-BRIGHT-1"]
    assert row.status is Status.SKIPPED and "автоэкспозицией" in row.reason


@needs_video
def test_scene_moved_after_restore_is_stand_error_with_expectation(camera_with, monkeypatch):
    # живая камера 09.11: сцена сдвинулась после возврата — проверка падала с TypeError
    calls = []

    def moved(*_args):
        calls.append(1)
        return "" if len(calls) == 1 else "средняя яркость ушла на 12 уровней"

    monkeypatch.setattr(engine, "moved", moved)
    row = rows(run(camera_with(), ["brightness"]))["IMAGE-BRIGHT-1"]
    assert row.status is Status.STAND_ERROR and row.reason.startswith("после возврата")
    assert row.expected


def unsettled_while(monkeypatch, holds):
    """Снимки нестабильны (срок `settle_max_s` вышел), пока `holds()` истинно; иначе — как есть."""
    original = frames.RtspFrameSource.capture

    def capture(self, *args, **kwargs):
        shot = original(self, *args, **kwargs)
        if holds():
            shot.stable = False
        return shot

    monkeypatch.setattr(frames.RtspFrameSource, "capture", capture)


@needs_video
def test_picture_not_settled_after_write_fails_and_is_restored(camera_with, monkeypatch):
    # ревью этапа 3, B-1: величина нестабильного кадра шла в verdict() как обычная —
    # ложное «прошла», хотя по спеке колеблется камера, а сцена стоит
    cam = camera_with()
    before = cam.state.imaging_xml
    unsettled_while(monkeypatch, lambda: re.search(r"<tt:Brightness>100\b", cam.state.imaging_xml))
    found = rows(run(cam, ["brightness", "saturation"]))
    row = found["IMAGE-BRIGHT-1"]
    assert row.status is Status.FAILED, row.reason
    assert "не успокоилась" in row.reason and "100 %" in row.reason and row.expected
    assert found["IMAGE-SAT-1"].status is Status.PASSED, found["IMAGE-SAT-1"].reason
    assert found[RESTORE_ID].status is Status.PASSED, found[RESTORE_ID].reason
    assert cam.state.imaging_xml == before


@needs_video
def test_before_frame_not_settled_is_stand_error_without_writes(camera_with, monkeypatch):
    # кадр «до» не успокоился без единой записи — колеблется сцена или свет, а не настройка
    cam = camera_with()
    unsettled_while(monkeypatch, lambda: not cam.state.imaging_writes)
    found = rows(run(cam, ["brightness"]))
    row = found["IMAGE-BRIGHT-1"]
    assert row.status is Status.STAND_ERROR and "не успокоилась" in row.reason, row.reason
    assert cam.state.imaging_writes == []
    assert found[RESTORE_ID].status is Status.SKIPPED


@needs_video
def test_after_frame_not_settled_is_stand_error(camera_with, monkeypatch):
    # кадр «после» не успокоился — неподвижность сцены не доказана, дефект камере не пишем
    cam = camera_with()
    unsettled_while(
        monkeypatch,
        lambda: (
            cam.state.imaging_writes
            and re.search(r"<tt:Brightness>50(\.0+)?<", cam.state.imaging_xml)
        ),
    )
    found = rows(run(cam, ["brightness"]))
    row = found["IMAGE-BRIGHT-1"]
    assert row.status is Status.STAND_ERROR and "после возврата" in row.reason, row.reason
    assert found[RESTORE_ID].status is Status.PASSED, found[RESTORE_ID].reason


@needs_video
def test_camera_changing_exposure_after_write_stops_check_and_blocks(camera_with):
    # Hikvision .111, 09.11: после записи настроек картинки камера сама сбросила MaxGain
    # экспозиции и вернуть его не дала, а возврат показал «прошла»
    cam = camera_with(imaging_sticky={"MinExposureTime": "33"})
    found = rows(run(cam, ["brightness", "saturation"]))
    first = found["IMAGE-BRIGHT-1"]
    assert first.status is Status.FAILED and "камера сама изменила" in first.reason
    assert "MinExposureTime 10" in first.reason and "MinExposureTime 33" in first.reason
    assert found["IMAGE-SAT-1"].status is Status.STAND_ERROR
    restore = found[RESTORE_ID]
    assert restore.status is Status.FAILED and "Exposure" in restore.reason
    writes = [w for w in cam.state.imaging_writes if "<tt:ColorSaturation>" in w]
    assert all("<tt:ColorSaturation>50<" in w for w in writes)  # насыщенность не писалась


# --- T11: возврат, журнал, пропажа камеры ------------------------------------------------------


def journal_entry(**fields):
    from qa_tool.core.write_journal import PendingWrite

    base = dict(
        run_id="QA-OLD",
        slot=1,
        address="127.0.0.1",
        port=0,
        vendor="generic",
        page="http://cam/onvif/imaging",
        label="Контраст",
        kind="contrast",
        old="50",
        new="100",
        stage="sent",
        restore={"token": "vs-1", "xml": MANUAL},
    )
    return PendingWrite(**(base | fields))


@needs_video
def test_write_left_by_previous_run_is_returned_with_checkbox(camera_with, tmp_path):
    from qa_tool.core.write_journal import IMAGE, RUN_ID_KEY, load, save

    cam = camera_with(xml=MANUAL.replace("<tt:Contrast>50<", "<tt:Contrast>100<"))
    save(tmp_path, journal_entry(port=cam.port))
    extra = {IMAGE.folder_key: str(tmp_path), RUN_ID_KEY: "QA-NEW"}
    found = rows(run(cam, ["saturation"], **extra))
    old = found[IMAGE.lost_spec.id]
    assert old.status is Status.PASSED and "возвращена: 100 → 50" in old.reason
    assert "<tt:Contrast>50</tt:Contrast>" in cam.state.imaging_xml
    assert load(tmp_path, "127.0.0.1", cam.port) is None


@needs_video
def test_write_left_by_previous_run_without_checkbox_is_a_blocker(camera_with, tmp_path):
    from qa_tool.core.write_journal import IMAGE, RUN_ID_KEY, save

    cam = camera_with(xml=MANUAL.replace("<tt:Contrast>50<", "<tt:Contrast>100<"))
    save(tmp_path, journal_entry(port=cam.port))
    extra = {IMAGE.folder_key: str(tmp_path), RUN_ID_KEY: "QA-NEW"}
    result = run(cam, ["saturation"], allow_writes=False, **extra)
    old = next(c for c in result.children if c.check_id == IMAGE.lost_spec.id)
    assert old.result.status is Status.FAILED and old.severity.value == "блокер"
    assert "вернуть нельзя" in old.result.reason
    assert cam.state.imaging_writes == []


@needs_video
def test_camera_lost_midway_keeps_rows_and_journal(camera_with, tmp_path):
    from qa_tool.core.write_journal import IMAGE, RUN_ID_KEY, load

    cam = camera_with()
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    limits = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
    limits["image.reconnect_wait_s"] = Threshold(1.0, "с", "тест")
    extra = {
        engine.SETTINGS_KEY: ["brightness", "saturation", "sharpness"],
        IMAGE.folder_key: str(tmp_path),
        RUN_ID_KEY: "QA-NOW",
    }
    ctx = CheckContext(target, "admin", Place.COLOR_BOX, limits, extra, allow_writes=True)

    def progress(text):
        if text.startswith("Насыщенность · 50"):
            cam.stop()

    ctx.progress = progress
    found = rows(run_check(SPEC, ctx))
    assert found["IMAGE-BRIGHT-1"].status is Status.PASSED
    assert found["IMAGE-SAT-1"].status is Status.FAILED
    assert "перестала отвечать" in found["IMAGE-SAT-1"].reason
    assert found["IMAGE-SHARP-1"].status is Status.STAND_ERROR
    restore = found[RESTORE_ID]
    assert restore.status is Status.FAILED and "не вернулась" in restore.reason
    kept = load(tmp_path, "127.0.0.1", cam.port)
    assert kept is not None and kept.kind == "saturation"
