"""CODEC-QUALITY-1 на виртуальной камере (этап 8, T9): эталон, ступени двух кодеков, итоги,
камера не приняла значение, возврат кодировщика."""

import json

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.codec import ORDER, engine
from qa_tool.checks.codec.engine import CHECK_ID, RESTORE_ID, STATE_ID, label
from qa_tool.checks.codec.quality import has_libvmaf
from qa_tool.core.checks import REGISTRY, CheckContext, Phase, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.onvif_generic import EncoderSettings
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState, encoder_now

load_all()
SPEC = REGISTRY.get(CHECK_ID)
needs_vmaf = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None
    or tool_path("ffprobe") is None
    or not has_libvmaf(tool_path("ffmpeg") or ""),
    reason="нет ffmpeg с libvmaf (он есть в ffmpeg сборки из кэша)",
)
HEVC = mock_rtsp.hevc_available()
CODECS = ("H264", "H265") if HEVC else ("H264",)
# Ролик mock — 10 к/с, GOP 10: выборка 2 с — две группы кадров, 20 кадров
FAST = {
    "codec.apply_delay_s": 0.0,
    "codec.restart_wait_s": 0.5,
    "codec.keyframe_wait_s": 5.0,
    "codec.clip_s": 2.0,
    "codec.clip_max_s": 2.0,
    "codec.frames": 15.0,
    "codec.min_frames": 10.0,
    "codec.grid_steps": 2.0,  # 320×240: ступени 2 и 1 Мбит/с
}


@pytest.fixture
def camera_with():
    started = []

    def start(**state):
        state.setdefault("encoder_codecs", CODECS)
        cam = MockCamera(
            Passport("X", "M", "1", "S1"), state=OnvifState(encoder_video=True, **state)
        ).start()
        started.append(cam)
        return cam

    yield start
    for cam in started:
        cam.stop()


def context(cam, extra=None, allow_writes=True, **limits):
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    values = {**FAST, **limits}
    thresholds = {k: Threshold(v, "", "тест") for k, v in values.items()}
    return CheckContext(
        target, "admin", Place.COLOR_BOX, thresholds, extra or {}, allow_writes=allow_writes
    )


def run(cam, allow_writes=True, **limits):
    return run_check(SPEC, context(cam, None, allow_writes, **limits))


def rows(result):
    return {c.check_id: c for c in result.children}


def settings_of(cam):
    stream = cam.state.streams[0] if cam.state.streams else MockStream(cam.state.profile_token)
    return encoder_now(cam.state, stream)


def test_registration_after_image_in_sync_phase():
    image = REGISTRY.get("IMAGE-EFFECT-1")
    assert SPEC.phase is Phase.SYNC and not SPEC.writes and SPEC.read_only_note
    assert SPEC.places == frozenset({Place.COLOR_BOX}) and SPEC.timeout_s >= 3600
    assert image.order < SPEC.order == ORDER and SPEC.help_id == "codec"


def test_label_shows_codec_rate_and_mode():
    assert label(EncoderSettings("H265", kbps=4096, constant=True)) == "H.265 4096 кбит/с пост."
    assert label(EncoderSettings("H264", kbps=None, constant=None)).endswith("режим не сообщён")


@needs_vmaf
def test_healthy_encoder_passes_both_codecs_and_is_restored(camera_with):
    cam = camera_with()
    before = settings_of(cam)
    ctx = context(cam)
    said: list[str] = []
    ctx.progress = said.append
    found = rows(run_check(SPEC, ctx))
    assert "Профиль 1 из 1 · H.264 · 2 Мбит/с · ступень 1 из 2" in said
    assert "Профиль 1 из 1 · эталон в начале · H.264 · 8 Мбит/с" in said
    assert any(line.startswith("Профиль 1 из 1 · расчёт качества · 1 из ") for line in said)
    for codec in CODECS:
        row = found[f"CODEC-{codec}-1"].result
        assert row.status is Status.PASSED, (codec, row.reason)
        names = {e.name for e in row.evidence}
        assert {"ref.jpg", "s1.jpg", "s2.jpg", "measure.json", "rtsp.txt"} <= names
        record = json.loads(next(e.content for e in row.evidence if e.name == "measure.json"))
        steps = [s["задано"] for s in record["ступени"]]
        assert {2048, 1024} <= set(steps), steps
        assert all(s["качество"]["vmaf"] for s in record["ступени"])
        assert record["предел замера"]["vmaf"][0] >= 90  # повтор эталона совпал: сцена держалась
        assert "уточнить" in row.threshold.source and "VMAF" in row.actual
    assert found[STATE_ID].result.status is Status.PASSED
    assert found[RESTORE_ID].result.status is Status.PASSED, found[RESTORE_ID].result.reason
    after = settings_of(cam)
    assert (after["encoding"], after["kbps"], after["constant"]) == (
        before["encoding"],
        before["kbps"],
        before["constant"],
    )
    if HEVC:
        assert cam.state.stream_restarts >= 2  # смена на H.265 и обратно


@needs_vmaf
def test_bitrate_off_fails_with_measured_value(camera_with):
    cam = camera_with(encoder_kbps_factor=2.0, encoder_codecs=("H264",))
    row = rows(run(cam))["CODEC-H264-1"].result
    assert row.status is Status.FAILED
    assert "битрейт не соответствует заданному" in row.reason
    assert any(e.name.endswith(".h264") for e in row.evidence)  # выборки у «не прошла»


@needs_vmaf
def test_quality_not_growing_with_bitrate_fails(camera_with):
    # качество ролика — на минимуме камеры: 32 кбит/с для 320×240 — заметно хуже эталона
    cam = camera_with(
        encoder_flat_quality=True, encoder_codecs=("H264",), encoder_kbps_range=(32, 8192)
    )
    # OpenH264 (ffmpeg Windows) держит VMAF ~87 даже на 32 кбит/с: порог выше «плоской» кривой,
    # но ниже эталона
    row = rows(run(cam, **{"codec.h264.sd.vmaf_min": 95.0}))["CODEC-H264-1"].result
    assert row.status is Status.FAILED
    assert "качество ниже порога" in row.reason or "не растёт" in row.reason, row.reason


@needs_vmaf
def test_stream_not_back_after_codec_switch_fails_with_onvif_alive(camera_with):
    cam = camera_with(encoder_codecs=("H264", "H265"), encoder_dead_codecs=("H265",))
    found = rows(run(cam))
    row = found["CODEC-H265-1"].result
    assert row.status is Status.FAILED and "поток не вернулся" in row.reason, row.reason
    assert found["CODEC-H264-1"].result.status is Status.PASSED
    assert found[RESTORE_ID].result.status is Status.PASSED


@needs_vmaf
def test_camera_keeping_its_bitrate_did_not_accept_value(camera_with):
    cam = camera_with(encoder_sticky_kbps=4000, encoder_codecs=("H264",))
    found = rows(run(cam))
    row = found["CODEC-H264-1"].result
    assert row.status is Status.FAILED and "камера не приняла значение" in row.reason
    assert found[RESTORE_ID].result.status is Status.PASSED


@needs_vmaf
def test_fault_on_write_fails_row_and_nothing_is_left_changed(camera_with):
    cam = camera_with(encoder_reject=True, encoder_codecs=("H264",))
    found = rows(run(cam))
    row = found["CODEC-H264-1"].result
    assert row.status is Status.FAILED and "камера не приняла" in row.reason
    assert found[RESTORE_ID].result.status is Status.PASSED


@needs_vmaf
def test_undeclared_second_codec_is_skipped(camera_with):
    cam = camera_with(encoder_codecs=("H264",))
    found = rows(run(cam))
    assert found["CODEC-H265-1"].result.status is Status.SKIPPED
    assert found["CODEC-H264-1"].result.status is Status.PASSED, found["CODEC-H264-1"].result.reason


def test_without_checkbox_nothing_is_written(camera_with):
    cam = camera_with()
    found = rows(run(cam, allow_writes=False))
    assert cam.state.encoder_writes == []
    for codec in ("H264", "H265"):
        row = found[f"CODEC-{codec}-1"].result
        assert row.status is Status.SKIPPED and "не разрешено сценарием" in row.reason
    assert found[STATE_ID].result.status is Status.PASSED
    assert found[RESTORE_ID].result.status is Status.SKIPPED


def test_broken_thresholds_are_stand_error_without_writes(camera_with):
    cam = camera_with()
    found = rows(run(cam, **{"codec.vmaf_spread": 0.0}))
    assert cam.state.encoder_writes == []
    row = found["CODEC-H264-1"].result
    assert row.status is Status.STAND_ERROR and "thresholds.yaml" in row.reason


def test_state_row_has_current_frame_and_bitrate_without_checkbox(camera_with):
    if mock_rtsp.find_ffmpeg() is None or tool_path("ffprobe") is None:
        pytest.skip("нет ffmpeg")
    cam = camera_with()
    state = rows(run(cam, allow_writes=False))[STATE_ID].result
    names = {e.name for e in state.evidence}
    assert {"state-1.jpg", "state.txt", "encoder-1.xml"} <= names
    text = next(e.content for e in state.evidence if e.name == "state.txt")
    assert "в потоке" in text and "H.264 4000 кбит/с перем." in text


def test_encoders_beyond_four_are_skipped(camera_with):
    streams = tuple(MockStream(f"p{i}", path=f"/p{i}") for i in range(1, 6))
    cam = camera_with(streams=streams)
    found = rows(run(cam, allow_writes=False))
    assert found["CODEC-H264-4"].result.status is Status.SKIPPED
    assert "первые 4" in found["CODEC-H264-5"].result.reason
    assert "первые 4" not in found["CODEC-H264-4"].result.reason


def test_out_of_time_leaves_rows_as_stand_error_and_writes_nothing(camera_with, monkeypatch):
    monkeypatch.setattr(engine, "TIMEOUT_S", 0.0)
    cam = camera_with(encoder_codecs=("H264",))
    found = rows(run(cam))
    row = found["CODEC-H264-1"].result
    assert row.status is Status.STAND_ERROR and engine.OUT_OF_TIME in row.reason
    assert cam.state.encoder_writes == []  # срок проверяется до записи
    assert found[RESTORE_ID].result.status is Status.SKIPPED


@needs_vmaf
def test_three_encoders_get_own_rows_and_all_are_restored(camera_with):
    streams = (
        MockStream("main", path="/live"),
        MockStream("sub", path="/sub", kbps=1000),
        MockStream("third", path="/third", kbps=512),
    )
    cam = camera_with(streams=streams, encoder_codecs=("H264",))
    found = rows(run(cam))
    for number in (1, 2, 3):
        row = found[f"CODEC-H264-{number}"].result
        assert row.status is Status.PASSED, (number, row.reason)
    assert found[RESTORE_ID].result.status is Status.PASSED, found[RESTORE_ID].result.reason
    for stream in streams:
        now = encoder_now(cam.state, stream)
        assert (now["encoding"], now["kbps"]) == ("H264", stream.kbps)


# --- T11: журнал прошлого прогона, пропажа камеры, сцена ----------------------------------------


def original_xml(cam) -> str:
    """Конфигурация кодировщика камеры, как её снимает проверка (доказательство encoder-1.xml)."""
    state = rows(run(cam, allow_writes=False))[STATE_ID].result
    return next(e.content for e in state.evidence if e.name == "encoder-1.xml")


def leave_previous_write(cam, folder) -> None:
    from qa_tool.core.write_journal import PendingWrite, save

    xml = original_xml(cam)
    cam.state.encoder_writes.clear()
    cam.state.encoder_now["main"] = {"encoding": "H264", "kbps": 2048, "constant": True}
    entry = PendingWrite(
        "QA-OLD",
        1,
        "127.0.0.1",
        cam.port,
        "generic",
        "http://cam/onvif/media2",
        "кодировщик main",
        "ve-main",
        "H.264 4000 кбит/с перем.",
        "H.264 2048 кбит/с пост.",
        "sent",
        restore={"token": "ve-main", "service": "media2", "xml": xml},
    )
    save(folder, entry)


def test_write_left_by_previous_run_is_returned_with_checkbox(camera_with, tmp_path):
    from qa_tool.core.write_journal import CODEC, RUN_ID_KEY, load

    cam = camera_with(encoder_codecs=("H264",))
    leave_previous_write(cam, tmp_path)
    extra = {CODEC.folder_key: str(tmp_path), RUN_ID_KEY: "QA-NEW"}
    # негодные пороги: после журнала проверка не пишет — тест смотрит только возврат журнала
    result = run_check(SPEC, context(cam, extra, **{"codec.vmaf_spread": 0.0}))
    old = rows(result)[CODEC.lost_spec.id].result
    assert old.status is Status.PASSED and "возвращена" in old.reason, old.reason
    now = settings_of(cam)
    assert (now["kbps"], now["constant"]) == (4000, False)
    assert load(tmp_path, "127.0.0.1", cam.port) is None


def test_write_left_by_previous_run_without_checkbox_is_a_blocker(camera_with, tmp_path):
    from qa_tool.core.write_journal import CODEC, RUN_ID_KEY

    cam = camera_with(encoder_codecs=("H264",))
    leave_previous_write(cam, tmp_path)
    extra = {CODEC.folder_key: str(tmp_path), RUN_ID_KEY: "QA-NEW"}
    found = rows(run_check(SPEC, context(cam, extra, allow_writes=False)))
    old = found[CODEC.lost_spec.id]
    assert old.result.status is Status.FAILED and old.severity.value == "блокер"
    assert "вернуть нельзя" in old.result.reason
    assert cam.state.encoder_writes == []


@needs_vmaf
def test_camera_lost_midway_keeps_rows_and_journal(camera_with, tmp_path):
    from qa_tool.core.write_journal import CODEC, RUN_ID_KEY, load

    cam = camera_with(encoder_codecs=("H264",))
    extra = {CODEC.folder_key: str(tmp_path), RUN_ID_KEY: "QA-NOW"}
    ctx = context(cam, extra, **{"image.reconnect_wait_s": 1.0})

    def progress(text):
        if "ступень 1" in text:
            cam.stop()

    ctx.progress = progress
    found = rows(run_check(SPEC, ctx))
    row = found["CODEC-H264-1"].result
    assert row.status is Status.FAILED and "перестала отвечать" in row.reason
    restore = found[RESTORE_ID].result
    assert restore.status is Status.FAILED and "не вернулась" in restore.reason
    kept = load(tmp_path, "127.0.0.1", cam.port)
    assert kept is not None and kept.kind == "ve-main"


@needs_vmaf
def test_moved_scene_is_stand_error_not_encoder_defect(camera_with, monkeypatch):
    monkeypatch.setattr(engine, "moved", lambda *_args: "средняя яркость ушла на 12 уровней")
    cam = camera_with(encoder_codecs=("H264",))
    found = rows(run(cam))
    row = found["CODEC-H264-1"].result
    assert row.status is Status.STAND_ERROR and "сцена менялась" in row.reason
    assert found[RESTORE_ID].result.status is Status.PASSED


def test_low_disk_is_stand_error_before_first_write(camera_with, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda _path: usage(1, 1, 100 << 20))
    cam = camera_with(encoder_codecs=("H264",))
    row = rows(run(cam))["CODEC-H264-1"].result
    assert row.status is Status.STAND_ERROR and "мало места" in row.reason
    assert cam.state.encoder_writes == []
