import pytest

from qa_tool.core.config import load_stand
from qa_tool.core.models import Camera, ConfigError, Place
from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.screens.cameras import NO_FREE_SLOTS, CamerasScreen
from qa_tool.gui.screens.stand import StandScreen
from qa_tool.gui.state import AppState
from tests.mock_camera import start_fleet


@pytest.fixture
def window(qtbot, tmp_path):
    create_app()
    w = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(w)
    w.show()
    return w


def screen(window, cls):
    return next(s for s in window.screens if isinstance(s, cls))


def test_eight_cameras_placed_ninth_rejected(window, tmp_path, memory_keyring):
    cameras = screen(window, CamerasScreen)
    for slot in range(1, 9):
        cameras.place_camera(Camera(slot=slot, address=f"10.0.0.{slot}", username="admin"), "pw")
    with pytest.raises(ConfigError) as error:
        cameras.place_camera(Camera(slot=1, address="10.0.0.9", username="admin"), "pw")
    assert str(error.value) == NO_FREE_SLOTS
    assert "камер на местах: 8 из 8" in window.status.text()
    text = (tmp_path / "stand.yaml").read_text(encoding="utf-8")
    assert "pw" not in text and "password" not in text
    assert len(load_stand(tmp_path / "stand.yaml").cameras) == 8
    assert memory_keyring.store[("qa-tool", "10.0.0.3:80")] == "pw"
    assert cameras.slot_labels[3].text().startswith("10.0.0.3")


def test_remove_camera_frees_slot(window, tmp_path):
    cameras = screen(window, CamerasScreen)
    cameras.place_camera(Camera(slot=2, address="10.0.0.2", username="admin"), "pw")
    cameras.remove_camera(2)
    assert cameras.slot_labels[2].text() == "свободно"
    assert load_stand(tmp_path / "stand.yaml").cameras == []


def test_place_chosen_on_stand_screen_is_saved(window, tmp_path):
    stand = screen(window, StandScreen)
    stand.place.setCurrentIndex(stand.place.findData(Place.DARK_ROOM.value))
    assert load_stand(tmp_path / "stand.yaml").place is Place.DARK_ROOM
    assert "тёмная комната" in window.status.text()


def test_find_cameras_fills_table(window, qtbot):
    fleet, responder = start_fleet(9, with_discovery=True)
    try:
        window.state.stand.interface = "127.0.0.1"
        cameras = screen(window, CamerasScreen)
        cameras.find_cameras()
        qtbot.waitUntil(lambda: cameras.find_button.isEnabled(), timeout=10000)
        ports = {int(cameras.table.item(r, 1).text()) for r in range(cameras.table.rowCount())}
        assert {m.port for m in fleet} <= ports
    finally:
        responder.stop()
        for cam in fleet:
            cam.stop()
