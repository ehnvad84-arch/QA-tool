"""Данные для входа в таблице экрана «Камеры» (docs/work/cameras-table, S1–S7)."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from qa_tool.core.config import load_stand
from qa_tool.core.models import Camera
from qa_tool.drivers.camera.discovery import FoundCamera
from qa_tool.gui.app import create_app
from qa_tool.gui.camera_dialog import CameraDialog, vendor_from_manufacturer
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.screens.cameras import (
    LAN_ADDRESS,
    PASSWORD,
    PORT,
    RTSP_PORT,
    SLOT,
    USERNAME,
    VENDOR,
    CamerasScreen,
)
from qa_tool.gui.state import AppState

FOUND = FoundCamera("192.0.2.66", 80, [], manufacturer="Ruware-Hisi", model="NVT")


@pytest.fixture
def cameras(qtbot, tmp_path, memory_keyring):
    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    screen = next(s for s in window.screens if isinstance(s, CamerasScreen))
    screen.warnings = []
    screen.warn = lambda _title, text: screen.warnings.append(text)
    return screen


def type_in(screen, column, text, row=0):
    screen.table.item(row, column).setText(text)


def test_s1_password_in_dialog_is_plain_text(qtbot):
    dialog = CameraDialog([1])
    qtbot.addWidget(dialog)
    assert dialog.password.echoMode() == QLineEdit.EchoMode.Normal


@pytest.mark.parametrize(
    ("passport", "vendor"),
    [
        ("Ruware-Hisi", "ruver"),
        ("Рувер", "ruver"),
        ("HIKVISION", "hikvision"),
        ("Dahua Technology", "dahua"),
        ("", "generic"),
        ("Noname", "generic"),
    ],
)
def test_s7_vendor_from_onvif_passport(passport, vendor):
    assert vendor_from_manufacturer(passport) == vendor


def test_s7_found_ruware_camera_gets_ruver_in_table(cameras):
    cameras._on_found([FOUND])
    assert cameras.table.cellWidget(0, VENDOR).currentText() == "ruver"
    assert cameras.table.item(0, 7).text() == "Ruware-Hisi NVT"


def test_s2_login_data_typed_in_table_go_to_stand(cameras, tmp_path, memory_keyring):
    cameras._on_found([FOUND])
    type_in(cameras, USERNAME, "operator")
    type_in(cameras, PASSWORD, "test-secret-1")
    type_in(cameras, RTSP_PORT, "8554")
    type_in(cameras, LAN_ADDRESS, "192.168.1.66")
    cameras.choose_slot(0, 3)

    camera = load_stand(tmp_path / "stand.yaml").camera_at(3)
    assert (camera.address, camera.username, camera.rtsp_port) == ("192.0.2.66", "operator", 8554)
    assert camera.vendor == "ruver" and camera.lan_address == "192.168.1.66"
    assert memory_keyring.store[("qa-tool", "192.0.2.66:80")] == "test-secret-1"
    assert "test-secret-1" not in (tmp_path / "stand.yaml").read_text(encoding="utf-8")
    assert cameras.slot_labels[3].text() == "Ruware-Hisi NVT\n192.0.2.66:80"
    assert cameras.table.cellWidget(0, SLOT).currentText() == "3"
    assert cameras.table.item(0, PASSWORD).text() == "test-secret-1"


def test_s2_slot_chosen_in_table_list_places_camera(cameras, qtbot, tmp_path):
    cameras._on_found([FOUND])
    box = cameras.table.cellWidget(0, SLOT)
    box.setCurrentIndex(box.findData(2))
    qtbot.waitUntil(lambda: cameras.state.stand.camera_at(2) is not None, timeout=2000)


def test_s3_edit_of_placed_camera_updates_stand_and_password(cameras, tmp_path, memory_keyring):
    cameras.place_camera(Camera(slot=1, address="192.0.2.10", username="admin"), "test-secret-1")
    row = next(i for i, r in enumerate(cameras.rows) if r.slot == 1)
    assert cameras.table.item(row, PASSWORD).text() == "test-secret-1"

    type_in(cameras, PASSWORD, "test-secret-2", row)
    type_in(cameras, USERNAME, "operator", row)
    type_in(cameras, PORT, "8080", row)
    cameras.table.cellWidget(row, VENDOR).setCurrentText("dahua")

    camera = load_stand(tmp_path / "stand.yaml").camera_at(1)
    assert (camera.username, camera.port, camera.vendor) == ("operator", 8080, "dahua")
    assert memory_keyring.store[("qa-tool", "192.0.2.10:8080")] == "test-secret-2"
    assert cameras.slot_labels[1].text() == "Dahua\n192.0.2.10:8080"


def test_s4_move_to_other_slot_and_take_off(cameras, tmp_path):
    cameras._on_found([FOUND])
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras.choose_slot(0, 1)
    cameras.choose_slot(0, 5)
    stand = load_stand(tmp_path / "stand.yaml")
    assert [c.slot for c in stand.cameras] == [5]
    assert cameras.slot_labels[1].text() == "свободно"

    cameras.choose_slot(0, None)
    assert load_stand(tmp_path / "stand.yaml").cameras == []
    assert cameras.table.cellWidget(0, SLOT).currentText() == "—"
    assert cameras.table.item(0, PASSWORD).text() == "test-secret-1"


def test_s4_taken_slot_not_offered(cameras):
    cameras.place_camera(Camera(slot=1, address="192.0.2.10", username="admin"), "test-secret-1")
    cameras._on_found([FOUND])
    row = next(i for i, r in enumerate(cameras.rows) if r.address == FOUND.address)
    box = cameras.table.cellWidget(row, SLOT)
    assert box.findData(1) == -1 and box.findData(2) != -1


@pytest.mark.parametrize(
    ("column", "bad", "message"),
    [
        (PORT, "80a", "Порт — число от 1 до 65535"),
        (RTSP_PORT, "70000", "Порт — число от 1 до 65535"),
        (LAN_ADDRESS, "a/b", "адрес за туннелем"),
    ],
)
def test_s5_bad_value_explained_and_reverted(cameras, column, bad, message):
    cameras._on_found([FOUND])
    before = cameras.table.item(0, column).text()
    type_in(cameras, column, bad)
    assert message in cameras.warnings[0]
    assert cameras.table.item(0, column).text() == before


def test_s5_bad_value_on_placed_camera_keeps_stand(cameras, tmp_path):
    cameras.place_camera(Camera(slot=2, address="192.0.2.10", username="admin"), "test-secret-1")
    type_in(cameras, 1, "")  # адрес не правится: ячейку подменили в обход редактора
    assert cameras.warnings == []
    assert load_stand(tmp_path / "stand.yaml").camera_at(2).address == "192.0.2.10"


def test_s6_new_search_keeps_typed_values(cameras):
    cameras._on_found([FOUND])
    type_in(cameras, USERNAME, "operator")
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras._on_found([FOUND])
    assert cameras.table.rowCount() == 1
    assert cameras.table.item(0, USERNAME).text() == "operator"
    assert cameras.table.item(0, PASSWORD).text() == "test-secret-1"


def test_manually_added_camera_appears_in_table(cameras):
    cameras.place_camera(
        Camera(slot=4, address="192.0.2.20", username="admin", rtsp_port=10554), "test-secret-1"
    )
    assert cameras.table.rowCount() == 1
    assert cameras.table.item(0, RTSP_PORT).text() == "10554"
    assert cameras.table.item(0, PASSWORD).text() == "test-secret-1"
    assert cameras.table.cellWidget(0, SLOT).currentText() == "4"


def test_slot_tile_shows_name_and_address(cameras, tmp_path):
    cameras._on_found([FOUND])
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras.choose_slot(0, 1)
    cameras.place_camera(
        Camera(slot=2, address="192.0.2.20", username="admin", vendor="ruver"), "test-secret-1"
    )
    cameras.place_camera(Camera(slot=3, address="192.0.2.30", username="admin"), "test-secret-1")
    assert cameras.slot_labels[1].text() == "Ruware-Hisi NVT\n192.0.2.66:80"
    assert cameras.slot_labels[2].text() == "Рувер\n192.0.2.20:80"
    assert cameras.slot_labels[3].text() == "марка не указана\n192.0.2.30:80"
    assert load_stand(tmp_path / "stand.yaml").camera_at(1).name == "Ruware-Hisi NVT"


def test_camera_markup_in_passport_shown_as_text(cameras):
    cameras._on_found([FoundCamera("192.0.2.5", 80, [], manufacturer="<b>X</b>")])
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras.choose_slot(0, 1)
    assert cameras.slot_labels[1].textFormat() == Qt.TextFormat.PlainText


# --- находки ревью круга 1 (docs/work/cameras-table/review.md) ---------------------------------

SECOND = FoundCamera("192.0.2.67", 80, [], manufacturer="Dahua", model="X1")
# та же камера за NAT на другом порту — своя строка
SAME_HOST = FoundCamera(FOUND.address, 8080, [], manufacturer="Dahua", model="X1")


def test_r1_same_address_and_port_cannot_appear_twice(cameras):
    cameras._on_found([FOUND, SAME_HOST])
    type_in(cameras, PORT, "80", row=1)
    assert "уже есть в таблице" in cameras.warnings[-1]
    assert cameras.table.item(1, PORT).text() == "8080"


def test_r1_slot_goes_to_the_row_it_was_chosen_in(cameras, tmp_path):
    cameras._on_found([FOUND, SECOND])
    type_in(cameras, USERNAME, "first", row=0)
    type_in(cameras, USERNAME, "second", row=1)
    type_in(cameras, PASSWORD, "test-secret-2", row=1)
    cameras.choose_slot(1, 3)
    assert cameras.rows[1].slot == 3 and cameras.rows[0].slot is None
    assert cameras.rows[0].username == "first"
    assert load_stand(tmp_path / "stand.yaml").camera_at(3).username == "second"


def test_r2_password_keeps_edge_spaces(cameras, memory_keyring):
    cameras._on_found([FOUND])
    type_in(cameras, PASSWORD, " test-secret-1 ")
    cameras.choose_slot(0, 1)
    assert memory_keyring.store[("qa-tool", "192.0.2.66:80")] == " test-secret-1 "


def test_r3_cleared_password_is_forgotten(cameras, memory_keyring):
    from qa_tool.core import secrets
    from qa_tool.core.models import ConfigError

    cameras.place_camera(Camera(slot=1, address="192.0.2.10", username="admin"), "test-secret-1")
    type_in(cameras, PASSWORD, "")
    assert ("qa-tool", "192.0.2.10:80") not in memory_keyring.store
    with pytest.raises(ConfigError):
        secrets.get_password(cameras.state.stand.camera_at(1))


def test_r4_address_is_not_editable(cameras):
    from PySide6.QtCore import Qt as QtCore

    cameras._on_found([FOUND])
    flags = cameras.table.item(0, 1).flags()
    assert not flags & QtCore.ItemFlag.ItemIsEditable


def test_r5_new_search_renames_placed_camera(cameras, tmp_path):
    cameras._on_found([FOUND])
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras.choose_slot(0, 2)
    renamed = FoundCamera(FOUND.address, FOUND.port, [], manufacturer="Ruware-Hisi", model="M2")
    cameras._on_found([renamed])
    assert load_stand(tmp_path / "stand.yaml").camera_at(2).name == "Ruware-Hisi M2"
    assert cameras.slot_labels[2].text().startswith("Ruware-Hisi M2")


@pytest.mark.parametrize("bad", ["user@host", "10.0.0.1 x"])
def test_s5_tunnel_address_with_at_or_space_refused(cameras, bad):
    cameras._on_found([FOUND])
    type_in(cameras, LAN_ADDRESS, bad)
    assert "адрес за туннелем" in cameras.warnings[0]
    assert cameras.table.item(0, LAN_ADDRESS).text() == ""


def test_s5_bad_port_on_placed_camera_keeps_stand(cameras, tmp_path):
    cameras.place_camera(Camera(slot=2, address="192.0.2.10", username="admin"), "test-secret-1")
    type_in(cameras, PORT, "0")
    assert load_stand(tmp_path / "stand.yaml").camera_at(2).port == 80


def test_s8_markup_in_search_name_is_text_on_run_card(qtbot, tmp_path, memory_keyring):
    from qa_tool.gui.screens.run import RunScreen

    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    cameras = next(s for s in window.screens if isinstance(s, CamerasScreen))
    cameras._on_found([FoundCamera("192.0.2.5", 80, [], manufacturer="<b>X</b>")])
    type_in(cameras, PASSWORD, "test-secret-1")
    cameras.choose_slot(0, 1)
    run = next(s for s in window.screens if isinstance(s, RunScreen))
    window.menu.setCurrentRow(3)
    run.cameras.list.setCurrentRow(0)
    assert run.card.model.text() == "<b>X</b>"
    assert run.card.model.textFormat() == Qt.TextFormat.PlainText
