from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QStyle,
    QWidget,
)

from qa_tool.gui.app import create_app
from qa_tool.gui.help import HELP_ID, TOOLTIP_DELAY_MS
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.state import AppState

CONTROLS = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractItemView)


def controls_without_help(root: QWidget) -> list[str]:
    missing = []
    for widget in root.findChildren(QWidget):
        visible_control = isinstance(widget, CONTROLS) and widget.isVisibleTo(root)
        if visible_control and not (widget.toolTip() and widget.property(HELP_ID)):
            label = widget.text() if hasattr(widget, "text") else widget.objectName()
            missing.append(f"{type(widget).__name__} «{label}»")
    return missing


def make_window(qtbot, tmp_path):
    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    window.show()
    return window


def test_tooltip_delay_is_one_second(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    assert window.style().styleHint(QStyle.StyleHint.SH_ToolTip_WakeUpDelay) == TOOLTIP_DELAY_MS


def test_every_control_on_every_screen_has_tooltip_and_help(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    for index in range(window.menu.count()):
        window.menu.setCurrentRow(index)
        assert window.menu.item(index).toolTip(), f"пункт меню {index} без подсказки"
        missing = controls_without_help(window)
        assert not missing, f"экран «{window.current_screen().title}»: без подсказки {missing}"


def test_control_without_tooltip_is_caught(qtbot, tmp_path):
    from PySide6.QtWidgets import QPushButton

    window = make_window(qtbot, tmp_path)
    button = QPushButton("Без подсказки")
    window.current_screen().layout_.addWidget(button)
    button.show()
    assert controls_without_help(window) == ["QPushButton «Без подсказки»"]


def test_five_screens_in_menu_order(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    titles = [window.menu.item(i).text() for i in range(window.menu.count())]
    assert titles == ["Стенд", "Камеры", "Сценарий", "Прогон", "Протоколы"]
    assert "камер на местах: 0 из 8" in window.status.text()
