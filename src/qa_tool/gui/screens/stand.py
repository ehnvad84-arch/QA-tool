"""Экран «Стенд»: место стенда, сетевая карта к камерам, папка прогонов, оборудование."""

from pathlib import Path

from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QWidget,
)

from qa_tool.core.models import Place
from qa_tool.gui import theme
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen

# Оборудование стенда появится на этапе 6; пока показываем, что оно не подключено.
RESOURCES = [
    ("Реле тревожных входов (8 каналов)", "этап 6"),
    ("Модуль дискретных входов (8 каналов)", "этап 6"),
    ("Звуковая карта, колонка, микрофон", "этап 6"),
    ("Управляемый свет", "этап 6"),
    ("Монитор для сцен аналитики", "этап 8"),
    ("Управление питанием PoE", "этап 6"),
]


def local_ipv4() -> list[str]:
    addresses = []
    for iface in QNetworkInterface.allInterfaces():
        for entry in iface.addressEntries():
            ip = entry.ip()
            if (
                ip.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
                and not ip.isLoopback()
            ):
                addresses.append(ip.toString())
    return addresses


class StandScreen(Screen):
    title = "Стенд"
    help_id = "stand"
    hint = "Место стенда, сетевая карта, папка прогонов и оборудование стенда"
    intro = "Где стоят камеры, через какую сетевую карту их искать и куда сохранять прогоны."

    def build(self) -> None:
        self._loading = True
        form = QFormLayout()
        self.place = helped(
            QComboBox(),
            "Цветовой бокс — картинка, цвет, кодеки, аналитика. Тёмная "
            "комната — ночь, ИК-подсветка, переход день/ночь. Программа предложит только "
            "проверки, доступные на выбранном месте.",
            "stand-place",
        )
        for place in Place:
            self.place.addItem(place.title, place.value)
        self.place.currentIndexChanged.connect(self._changed)

        self.interface = helped(
            QComboBox(),
            "Сетевая карта компьютера, подключённая к коммутатору с "
            "камерами. Через неё идёт поиск камер.",
            "stand-network",
        )
        self.interface.addItem("по умолчанию", "")
        for ip in local_ipv4():
            self.interface.addItem(ip, ip)
        self.interface.currentIndexChanged.connect(self._changed)

        runs_row = QWidget()
        row = QHBoxLayout(runs_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.runs_dir = helped(
            QLineEdit(),
            "Папка, куда сохраняются прогоны: база, доказательства и протоколы.",
            "stand-runs",
        )
        self.runs_dir.editingFinished.connect(self._changed)
        browse = helped(QPushButton("Выбрать…"), "Выбрать папку прогонов.", "stand-runs")
        browse.clicked.connect(self._browse)
        row.addWidget(self.runs_dir, 1)
        row.addWidget(browse)

        self.theme = helped(
            QComboBox(),
            "Цвета окна: светлая или тёмная тема. Окно перекрашивается сразу, "
            "выбор запоминается до следующего запуска.",
            "stand-theme",
        )
        for name, title in theme.TITLES.items():
            self.theme.addItem(title, name)
        self.theme.setCurrentIndex(self.theme.findData(theme.current_name()))
        # тема — настройка пользователя, а не стенда: в stand.yaml не пишется
        self.theme.currentIndexChanged.connect(
            lambda: theme.apply(QApplication.instance(), self.theme.currentData(), remember=True)
        )

        form.addRow("Место стенда", self.place)
        form.addRow("Сетевая карта к камерам", self.interface)
        form.addRow("Папка прогонов", runs_row)
        form.addRow("Тема окна", self.theme)
        self.layout_.addLayout(form)

        self.resources = helped(
            QListWidget(),
            "Оборудование стенда и его состояние. Подключается на этапах 6–8.",
            "stand-resources",
        )
        for name, stage in RESOURCES:
            self.resources.addItem(f"{name} — не подключено ({stage})")
        self.resources.setMaximumHeight(170)
        self.layout_.addWidget(self.resources)
        self.refresh()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Папка прогонов", self.runs_dir.text())
        if folder:
            self.runs_dir.setText(folder)
            self._changed()

    def _changed(self) -> None:
        if self._loading:
            return
        stand = self.state.stand
        stand.place = Place(self.place.currentData())
        stand.interface = self.interface.currentData() or ""
        stand.runs_dir = Path(self.runs_dir.text().strip() or stand.runs_dir)
        self.state.save()
        self.notify()

    def refresh(self) -> None:
        self._loading = True
        stand = self.state.stand
        self.place.setCurrentIndex(self.place.findData(stand.place.value))
        index = self.interface.findData(stand.interface)
        if index < 0:
            self.interface.addItem(stand.interface, stand.interface)
            index = self.interface.count() - 1
        self.interface.setCurrentIndex(index)
        self.runs_dir.setText(str(stand.runs_dir))
        self.theme.blockSignals(True)
        self.theme.setCurrentIndex(self.theme.findData(theme.current_name()))
        self.theme.blockSignals(False)
        self._loading = False
