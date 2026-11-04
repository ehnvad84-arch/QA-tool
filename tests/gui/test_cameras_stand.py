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


def test_broken_stand_file_set_aside_window_opens(tmp_path, monkeypatch):
    from qa_tool.gui import app as gui_app

    path = tmp_path / "stand.yaml"
    path.write_text("- это не набор настроек\n", encoding="utf-8")
    monkeypatch.setattr(gui_app, "default_stand_path", lambda: path)
    state, message = gui_app.load_state()
    assert "не прочитан" in message and state.stand.cameras == []
    assert list(tmp_path.glob("stand.yaml.broken-*"))


def test_find_button_reenabled_after_discovery_error(window, qtbot, monkeypatch):
    def boom(**_kwargs):
        raise ValueError("мусор в сети")

    monkeypatch.setattr("qa_tool.gui.screens.cameras.discover", boom)
    cameras = screen(window, CamerasScreen)
    cameras.find_cameras()
    qtbot.waitUntil(lambda: cameras.find_button.isEnabled(), timeout=5000)
    assert "мусор в сети" in cameras.search_state.text()


def test_without_password_storage_camera_is_added_with_warning(qtbot, tmp_path, memory_keyring):
    from keyring.errors import NoKeyringError

    from qa_tool.core import secrets
    from qa_tool.core.models import Camera
    from qa_tool.gui.app import create_app
    from qa_tool.gui.main_window import MainWindow
    from qa_tool.gui.screens.cameras import CamerasScreen
    from qa_tool.gui.state import AppState

    def refuse(*_):
        raise NoKeyringError("нет хранилища")

    memory_keyring.set_password = refuse
    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    screen = next(s for s in window.screens if isinstance(s, CamerasScreen))
    warnings = []
    screen.warn = lambda _title, text: warnings.append(text)
    camera = Camera(slot=1, address="10.0.0.7", username="admin")
    screen.place_camera(camera, "s3cret")
    assert window.state.stand.camera_at(1) is not None
    assert "нет системного хранилища" in warnings[0]
    assert secrets.get_password(camera) == "s3cret"
    assert "s3cret" not in (tmp_path / "stand.yaml").read_text(encoding="utf-8")
    secrets._session.clear()
