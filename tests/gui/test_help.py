from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

from qa_tool.gui.app import create_app
from qa_tool.gui.camera_dialog import CameraDialog
from qa_tool.gui.help import HELP_ID
from qa_tool.gui.help_window import load_topics
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.screens.cameras import CamerasScreen
from qa_tool.gui.state import AppState


def help_ids(root: QWidget) -> set[str]:
    ids = {root.property(HELP_ID)} | {w.property(HELP_ID) for w in root.findChildren(QWidget)}
    return {i for i in ids if i}


def make_window(qtbot, tmp_path):
    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    window.show()
    return window


def test_every_help_id_has_manual_section(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    dialog = CameraDialog([1])
    qtbot.addWidget(dialog)
    used = help_ids(window) | help_ids(dialog)
    missing = used - set(load_topics())
    assert not missing, f"нет раздела справки для: {sorted(missing)}"
    assert {"stand", "cameras", "scenario", "run", "protocols"} <= used


def test_f1_on_cameras_screen_opens_cameras_section(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.menu.setCurrentRow(1)
    cameras = window.current_screen()
    assert isinstance(cameras, CamerasScreen)
    topic = window.show_help(cameras)
    assert topic.help_id == "cameras" and topic.heading == "Камеры"
    assert "Камеры" in window.help_window.browser.toPlainText()


def test_f1_shortcut_uses_focused_element(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    window.menu.setCurrentRow(1)
    cameras = window.current_screen()
    monkeypatch.setattr("qa_tool.gui.main_window.QApplication.widgetAt", lambda _pos: None)
    monkeypatch.setattr(
        "qa_tool.gui.main_window.QApplication.focusWidget", lambda: cameras.find_button
    )
    # привязку F1 проверяем напрямую: доставка нажатия зависит от того, какое окно ОС активно
    f1 = next(s for s in window.findChildren(QShortcut) if s.key() == QKeySequence(Qt.Key.Key_F1))
    assert f1.context() == Qt.ShortcutContext.ApplicationShortcut
    f1.activated.emit()
    assert window.help_window is not None and window.help_window.isVisible()
    assert window.help_window.current.help_id == "cameras-find"
    assert window.help_window.windowTitle() == "Справка — Поиск камер"
