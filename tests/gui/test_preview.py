"""Превью потока на экране «Прогон» (docs/work/stream-preview)."""

import pytest

from qa_tool.core.models import Camera, Place
from qa_tool.gui import preview
from qa_tool.gui.preview import PreviewWorker
from qa_tool.gui.screens.run import PREVIEW_PAUSED, RunScreen
from tests.gui.test_scenario_run import make_window, screen
from tests.mock_camera import start_fleet


@pytest.fixture
def camera(monkeypatch):
    monkeypatch.setattr(preview, "ENABLED", True)
    monkeypatch.setattr(preview, "PERIOD_S", 0.2)
    cams, _ = start_fleet(1)
    monkeypatch.setenv("QA_TOOL_PASSWORD_1", "admin")
    yield cams[0]
    cams[0].stop()


def stand_camera(mock) -> Camera:
    return Camera(
        slot=1, address="127.0.0.1", port=mock.port, rtsp_port=mock.rtsp_port, username="admin"
    )


def test_frame_of_selected_camera_arrives(qtbot, camera):
    worker = PreviewWorker(Place.COLOR_BOX)
    try:
        with qtbot.waitSignal(worker.frame, timeout=30000) as got:
            worker.watch(stand_camera(camera))
            worker.set_paused(False)
        slot, jpeg = got.args
        assert slot == 1 and jpeg.startswith(b"\xff\xd8")
    finally:
        worker.stop()


def test_wrong_password_stops_preview_without_new_attempts(qtbot, camera, monkeypatch):
    monkeypatch.setenv("QA_TOOL_PASSWORD_1", "test-secret-wrong")
    worker = PreviewWorker(Place.COLOR_BOX)
    try:
        with qtbot.waitSignal(worker.status, timeout=30000) as got:
            worker.watch(stand_camera(camera))
            worker.set_paused(False)
        assert "не приняла логин или пароль" in got.args[1]
        assert "Превью остановлено" in got.args[1]
        sent = len(camera.requests)
        qtbot.wait(1500)  # несколько периодов превью
        assert len(camera.requests) == sent
    finally:
        worker.stop()


def test_paused_worker_does_not_touch_camera(qtbot, camera):
    worker = PreviewWorker(Place.COLOR_BOX)
    try:
        worker.watch(stand_camera(camera))
        qtbot.wait(1000)
        assert camera.requests == []
    finally:
        worker.stop()


def test_run_screen_pauses_preview_during_run(qtbot, tmp_path, camera):
    window = make_window(qtbot, tmp_path, [camera])
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    window.state.running = True
    run._watch_preview()
    assert run.preview._paused is True
    assert run.card.preview.text() == PREVIEW_PAUSED
    window.state.running = False
    run._watch_preview()
    assert run.preview._paused is False


def test_hidden_run_screen_pauses_preview(qtbot, tmp_path, camera):
    window = make_window(qtbot, tmp_path, [camera])
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    assert run.preview._paused is False
    window.menu.setCurrentRow(1)
    assert run.preview._paused is True
