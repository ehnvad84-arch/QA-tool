"""Экран «Камеры»: поиск в сети, добавление вручную, 8 мест стенда."""

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from qa_tool.core.models import MAX_SLOTS, Camera, ConfigError
from qa_tool.core.secrets import set_password
from qa_tool.drivers.camera.discovery import FoundCamera, discover
from qa_tool.gui.camera_dialog import CameraDialog, vendor_from_manufacturer
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen

NO_FREE_SLOTS = (
    "Свободных мест нет: на стенде уже 8 камер. Снимите камеру с места, чтобы поставить другую."
)


class _DiscoverySignals(QObject):
    done = Signal(list)
    failed = Signal(str)


class CamerasScreen(Screen):
    title = "Камеры"
    help_id = "cameras"
    hint = "Поиск камер в сети и расстановка по местам 1–8"
    intro = "Найдите камеры в сети или добавьте вручную и поставьте их на места стенда."

    def build(self) -> None:
        self.found: list[FoundCamera] = []
        self.signals = _DiscoverySignals()
        self.signals.done.connect(self._on_found)
        self.signals.failed.connect(self._on_discovery_failed)

        buttons = QHBoxLayout()
        self.find_button = helped(
            QPushButton("Найти камеры"),
            "Разослать запрос ONVIF в сеть стенда и показать ответившие камеры (до 10 секунд).",
            "cameras-find",
        )
        self.find_button.clicked.connect(self.find_cameras)
        self.add_button = helped(
            QPushButton("Добавить вручную…"),
            "Добавить камеру по IP-адресу, если "
            "она не нашлась поиском (другая подсеть, выключен поиск).",
            "cameras-add",
        )
        self.add_button.clicked.connect(lambda: self.open_dialog())
        self.place_button = helped(
            QPushButton("Поставить на место…"),
            "Поставить выбранную в таблице найденную камеру на свободное место стенда.",
            "cameras-add",
        )
        self.place_button.clicked.connect(self.place_selected)
        for b in (self.find_button, self.add_button, self.place_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.layout_.addLayout(buttons)

        self.search_state = QLabel("Поиск ещё не запускали.")
        self.layout_.addWidget(self.search_state)
        self.table = helped(
            QTableWidget(0, 5),
            "Камеры, ответившие на поиск. Выберите строку и нажмите «Поставить на место…».",
            "cameras-find",
        )
        self.table.setHorizontalHeaderLabels(
            ["Адрес", "Порт", "Производитель", "Модель", "На стенде"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(220)
        self.layout_.addWidget(self.table)

        self.layout_.addWidget(QLabel("<b>Места стенда</b>"))
        grid = QGridLayout()
        self.slot_labels: dict[int, QLabel] = {}
        self.remove_buttons: dict[int, QPushButton] = {}
        for slot in range(1, MAX_SLOTS + 1):
            box = QGroupBox(f"Место {slot}")
            inner = QVBoxLayout(box)
            label = QLabel()
            label.setWordWrap(True)
            remove = helped(
                QPushButton("Снять с места"),
                f"Убрать камеру с места {slot}. Пароль камеры останется в хранилище паролей.",
                "cameras-slots",
            )
            remove.clicked.connect(lambda _=False, s=slot: self.remove_camera(s))
            inner.addWidget(label)
            inner.addWidget(remove)
            self.slot_labels[slot], self.remove_buttons[slot] = label, remove
            grid.addWidget(box, (slot - 1) // 4, (slot - 1) % 4)
        self.layout_.addLayout(grid)
        self.refresh()

    # --- поиск ---------------------------------------------------------------------------------

    def find_cameras(self) -> None:
        self.find_button.setEnabled(False)
        self.search_state.setText("Идёт поиск…")
        interface = self.state.stand.interface

        def work():
            try:
                self.signals.done.emit(discover(timeout=3.0, interface=interface))
            except OSError as e:
                self.signals.failed.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_found(self, found: list) -> None:
        self.find_button.setEnabled(True)
        self.found = found
        self.search_state.setText(
            f"Найдено камер: {len(found)}"
            if found
            else "Ничего не найдено. Проверьте сетевую карту на экране «Стенд» "
            "или добавьте камеру вручную."
        )
        self._fill_table()

    def _on_discovery_failed(self, error: str) -> None:
        self.find_button.setEnabled(True)
        self.search_state.setText(f"Поиск не удался: {error}")

    def _fill_table(self) -> None:
        on_stand = {(c.address, c.port) for c in self.state.stand.cameras}
        self.table.setRowCount(len(self.found))
        for row, cam in enumerate(self.found):
            values = [
                cam.address,
                str(cam.port),
                cam.manufacturer,
                cam.model,
                "да" if (cam.address, cam.port) in on_stand else "нет",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    # --- места ---------------------------------------------------------------------------------

    def place_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "Камеры", "Сначала выберите камеру в таблице найденных.")
            return
        cam = self.found[rows[0].row()]
        self.open_dialog(cam.address, cam.port, vendor_from_manufacturer(cam.manufacturer))

    def open_dialog(self, address: str = "", port: int = 80, vendor: str = "generic") -> None:
        free = self.state.stand.free_slots()
        if not free:
            QMessageBox.warning(self, "Камеры", NO_FREE_SLOTS)
            return
        dialog = CameraDialog(free, address, port, vendor, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.place_camera(dialog.camera(), dialog.password.text())
            except ConfigError as e:
                QMessageBox.warning(self, "Камеры", str(e))

    def place_camera(self, camera: Camera, password: str) -> None:
        """Поставить камеру на место; ConfigError — если мест нет или место занято."""
        if not self.state.stand.free_slots():
            raise ConfigError(NO_FREE_SLOTS)
        self.state.stand.add_camera(camera)
        if password:
            set_password(camera, password)
        self.state.save()
        self.refresh()
        self.notify()

    def remove_camera(self, slot: int) -> None:
        self.state.stand.remove_camera(slot)
        self.state.save()
        self.refresh()
        self.notify()

    def refresh(self) -> None:
        for slot, label in self.slot_labels.items():
            cam = self.state.stand.camera_at(slot)
            label.setText(f"{cam.address}:{cam.port}\n{cam.vendor}" if cam else "свободно")
            self.remove_buttons[slot].setVisible(cam is not None)
        self._fill_table()
