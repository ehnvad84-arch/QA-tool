"""Общий вид экрана: заголовок, пояснение, место под содержимое."""

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from qa_tool.gui.help import HELP_ID


class Screen(QWidget):
    title = ""
    help_id = ""
    hint = ""  # подсказка пункта меню
    intro = ""  # строка под заголовком

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setProperty(HELP_ID, self.help_id)
        self.layout_ = QVBoxLayout(self)
        heading = QLabel(f"<h2>{self.title}</h2>")
        self.layout_.addWidget(heading)
        if self.intro:
            intro = QLabel(self.intro)
            intro.setWordWrap(True)
            self.layout_.addWidget(intro)
        self.build()
        self.layout_.addStretch(1)

    def build(self) -> None:
        """Содержимое экрана — в наследниках."""

    def refresh(self) -> None:
        """Обновить данные при показе экрана."""

    def notify(self) -> None:
        """Состояние стенда изменилось — обновить строку статуса окна."""
        window = self.window()
        if hasattr(window, "update_status"):
            window.update_status()
