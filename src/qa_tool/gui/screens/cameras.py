"""Экран «Камеры»: поиск в сети, данные для входа в таблице, 8 мест стенда."""

import functools
import threading
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from qa_tool.core.models import MAX_SLOTS, Camera, ConfigError
from qa_tool.core.secrets import forget_password, get_password, set_password
from qa_tool.drivers.camera.discovery import discover
from qa_tool.gui.camera_dialog import VENDORS, CameraDialog, vendor_from_manufacturer
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen

NO_FREE_SLOTS = (
    "Свободных мест нет: на стенде уже 8 камер. Снимите камеру с места, чтобы поставить другую."
)
MEMORY_ONLY = (
    "На этом компьютере нет системного хранилища паролей. Пароль будет работать "
    "до закрытия программы; после запуска камеру нужно добавить заново."
)

COLUMNS = [
    "Место",
    "Адрес",
    "HTTP-порт",
    "RTSP-порт",
    "Логин",
    "Пароль",
    "Производитель",
    "Паспорт ONVIF",
    "Адрес за туннелем",
]
SLOT, ADDRESS, PORT, RTSP_PORT, USERNAME, PASSWORD, VENDOR, PASSPORT, LAN_ADDRESS = range(
    len(COLUMNS)
)
# Текстовые колонки и поле строки за каждой; адрес — ключ строки, его не правят (спека, S2)
TEXT_FIELDS = {
    ADDRESS: "address",
    PORT: "port",
    RTSP_PORT: "rtsp_port",
    USERNAME: "username",
    PASSWORD: "password",
    LAN_ADDRESS: "lan_address",
}


@dataclass
class CameraRow:
    """Строка таблицы: камера и данные, с которыми программа в неё входит.

    Строка живёт на экране, пока он открыт: правки не теряются при повторном поиске.
    На стенд она попадает, только когда ей выбрано место (`slot`).
    """

    address: str
    port: int = 80
    rtsp_port: int = 554
    username: str = "admin"
    password: str = ""
    vendor: str = "generic"
    # Производитель и модель из ответа на поиск; на стенде — имя камеры в списке прогона
    passport: str = ""
    lan_address: str = ""
    slot: int | None = None

    def camera(self, slot: int) -> Camera:
        """Камера для места `slot`; ConfigError — если адрес пуст или неверен."""
        return Camera(
            slot=slot,
            address=self.address,
            port=self.port,
            rtsp_port=self.rtsp_port,
            username=self.username,
            vendor=self.vendor,
            name=self.passport,
            lan_address=self.lan_address,
        )


def _known_password(camera: Camera) -> str:
    try:
        return get_password(camera)
    except ConfigError:
        return ""


def _parse_port(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise ValueError(f"Порт — число от 1 до 65535, а не «{text}»")
    return int(text)


class _DiscoverySignals(QObject):
    done = Signal(list)
    failed = Signal(str)


class CamerasScreen(Screen):
    title = "Камеры"
    help_id = "cameras"
    hint = "Поиск камер в сети и расстановка по местам 1–8"
    intro = (
        "Найдите камеры в сети или добавьте вручную, впишите в таблицу логин и пароль "
        "и выберите место стенда."
    )

    def build(self) -> None:
        self.rows: list[CameraRow] = []
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
            QPushButton("Поставить на место"),
            "Поставить выбранную в таблице камеру на первое свободное место стенда. "
            "Другое место можно выбрать в колонке «Место».",
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
            QTableWidget(0, len(COLUMNS)),
            "Камеры из поиска и камеры на местах. Порты, логин, пароль, производителя и адрес "
            "за туннелем правьте прямо в строке, место выбирайте в колонке «Место». Это данные, "
            "с которыми программа входит в камеру: в самой камере они не меняются.",
            "cameras-find",
        )
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setMaximumHeight(260)
        self.table.itemChanged.connect(self._cell_edited)
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
            # Название приходит из ответа камеры: разметка в нём — текст, а не оформление
            label.setTextFormat(Qt.TextFormat.PlainText)
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
            except Exception as e:  # любая ошибка — сообщение, а не залипшая кнопка
                self.signals.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_found(self, found: list) -> None:
        self.find_button.setEnabled(True)
        self.search_state.setText(
            f"Найдено камер: {len(found)}"
            if found
            else "Ничего не найдено. Проверьте сетевую карту на экране «Стенд» "
            "или добавьте камеру вручную."
        )
        for cam in found:
            passport = " ".join(part for part in (cam.manufacturer, cam.model) if part)
            row = self._row_for(cam.address, cam.port)
            if row is None:
                vendor = vendor_from_manufacturer(cam.manufacturer)
                self.rows.append(CameraRow(cam.address, cam.port, vendor=vendor, passport=passport))
            elif row.slot is not None and row.passport != passport:
                self.edit_row(self.rows.index(row), passport=passport)  # название на стенде тоже
            else:
                row.passport = passport
        self.refresh()

    def _on_discovery_failed(self, error: str) -> None:
        self.find_button.setEnabled(True)
        self.search_state.setText(f"Поиск не удался: {error}")

    # --- таблица -------------------------------------------------------------------------------

    def _row_for(self, address: str, port: int) -> CameraRow | None:
        return next((r for r in self.rows if (r.address, r.port) == (address, port)), None)

    def _sync_rows(self) -> None:
        """Привязать строки к камерам стенда: место могли сменить плитка или окно «вручную»."""
        on_stand = {c.slot: c for c in self.state.stand.cameras}
        for row in self.rows:
            cam = on_stand.get(row.slot)
            if cam is None or (cam.address, cam.port) != (row.address, row.port):
                row.slot = None
        bound = {row.slot for row in self.rows}
        for cam in self.state.stand.cameras:
            if cam.slot in bound:
                continue
            row = self._row_for(cam.address, cam.port)
            if row is None or row.slot is not None:
                row = CameraRow(cam.address, cam.port)
                self.rows.append(row)
            row.slot = cam.slot
            row.rtsp_port, row.username = cam.rtsp_port, cam.username
            row.vendor, row.lan_address = cam.vendor, cam.lan_address
            row.passport = row.passport or cam.name
            row.password = _known_password(cam) or row.password

    def _fill_table(self) -> None:
        # Заполнение программой — не правка инженера: itemChanged молчит
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        free = self.state.stand.free_slots()
        for index, row in enumerate(self.rows):
            for column, field in TEXT_FIELDS.items():
                self.table.setItem(index, column, QTableWidgetItem(str(getattr(row, field))))
            self.table.setItem(index, PASSPORT, QTableWidgetItem(row.passport))
            for column in (ADDRESS, PASSPORT):
                item = self.table.item(index, column)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setCellWidget(index, SLOT, self._slot_box(index, row, free))
            self.table.setCellWidget(index, VENDOR, self._vendor_box(index, row))
        self.table.blockSignals(False)

    def _slot_box(self, index: int, row: CameraRow, free: list[int]) -> QComboBox:
        box = helped(
            QComboBox(),
            "Место камеры на стенде. «—» — камера не на месте; выбор номера ставит её туда "
            "с данными этой строки.",
            "cameras-slots",
        )
        box.addItem("—", 0)
        for slot in sorted(set(free) | ({row.slot} if row.slot else set())):
            box.addItem(str(slot), slot)
        box.setCurrentIndex(box.findData(row.slot or 0))
        # Смена места перестраивает таблицу и удаляет этот список, поэтому — после его сигнала
        box.currentIndexChanged.connect(
            lambda _i, b=box: QTimer.singleShot(
                0, functools.partial(self.choose_slot, index, b.currentData() or None)
            )
        )
        return box

    def _vendor_box(self, index: int, row: CameraRow) -> QComboBox:
        box = helped(
            QComboBox(),
            "Производитель: какой адаптер API добавить к общим проверкам. Нет в списке — "
            "«generic».",
            "cameras-add",
        )
        box.addItems(VENDORS)
        box.setCurrentText(row.vendor)
        box.currentTextChanged.connect(lambda vendor: self.edit_row(index, vendor=vendor))
        return box

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        field = TEXT_FIELDS.get(item.column())
        if field is None or item.column() == ADDRESS:
            return
        # пробел по краю пароля — часть пароля, а не опечатка
        text = item.text() if field == "password" else item.text().strip()
        try:
            value = _parse_port(text) if field in ("port", "rtsp_port") else text
        except ValueError as e:
            value = None
            self.warn("Камеры", str(e))
        if value is None or not self.edit_row(item.row(), **{field: value}):
            self.table.blockSignals(True)
            item.setText(str(getattr(self.rows[item.row()], field)))
            self.table.blockSignals(False)

    def edit_row(self, index: int, **changes) -> bool:
        """Поменять данные строки; у камеры на месте — сразу и на стенде.

        Args:
            index: номер строки таблицы.
            **changes: поля `CameraRow` с новыми значениями.

        Returns:
            False — значение неверно, инженер получил объяснение, строка не изменилась.
        """
        old = self.rows[index]
        row = replace(old, **changes)
        twin = self._row_for(row.address, row.port)
        if (row.address, row.port) != (old.address, old.port) and twin is not None:
            self.warn("Камеры", f"Камера {row.address}:{row.port} уже есть в таблице.")
            return False
        slot = row.slot or 1  # у строки без места адрес проверяется так же, как при постановке
        try:
            camera = row.camera(slot)
        except ConfigError as e:
            self.warn("Камеры", str(e).removeprefix(f"Место {slot}: "))
            return False
        self.rows[index] = row
        if row.slot is not None:
            self.state.stand.remove_camera(row.slot)
            self.state.stand.add_camera(camera)
            in_memory_only = False
            if row.password:
                in_memory_only = not set_password(camera, row.password)
            else:
                forget_password(camera)  # иначе прогон тихо шёл бы со стёртым из таблицы паролем
            self.state.save()
            self._refresh_slots()
            self.notify()
            if in_memory_only:
                self.warn("Камеры", MEMORY_ONLY)
        return True

    def choose_slot(self, index: int, slot: int | None) -> None:
        """Поставить камеру строки на место `slot`, переставить её или снять (`None`)."""
        row = self.rows[index]
        if slot == row.slot:
            return
        try:
            camera = row.camera(slot) if slot else None
            if camera and slot not in self.state.stand.free_slots():
                raise ConfigError(f"Место {slot} уже занято")
        except ConfigError as e:
            self.warn("Камеры", str(e))
            self.refresh()
            return
        if row.slot is not None:
            self.state.stand.remove_camera(row.slot)
        # Место привязывается к этой строке сразу: иначе `_sync_rows` отдал бы его первой
        # строке с тем же адресом
        row.slot = slot
        if camera:
            self.place_camera(camera, row.password)
        else:
            self.state.save()
            self.refresh()
            self.notify()

    # --- места ---------------------------------------------------------------------------------

    def place_selected(self) -> None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(self, "Камеры", "Сначала выберите камеру в таблице.")
            return
        row = self.rows[selected[0].row()]
        free = self.state.stand.free_slots()
        if row.slot is not None:
            QMessageBox.information(self, "Камеры", f"Камера уже стоит на месте {row.slot}.")
        elif not free:
            QMessageBox.warning(self, "Камеры", NO_FREE_SLOTS)
        else:
            self.choose_slot(selected[0].row(), free[0])

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

    def warn(self, title: str, text: str) -> None:
        """Показать предупреждение; тесты подставляют свою запись вместо окошка."""
        QMessageBox.warning(self, title, text)

    def place_camera(self, camera: Camera, password: str) -> None:
        """Поставить камеру на место; ConfigError — если мест нет или место занято."""
        if not self.state.stand.free_slots():
            raise ConfigError(NO_FREE_SLOTS)
        if camera.slot not in self.state.stand.free_slots():
            raise ConfigError(f"Место {camera.slot} уже занято")
        in_memory_only = bool(password) and not set_password(camera, password)
        self.state.stand.add_camera(camera)
        self.state.save()
        self.refresh()
        self.notify()
        if in_memory_only:
            self.warn("Камеры", MEMORY_ONLY)

    def remove_camera(self, slot: int) -> None:
        self.state.stand.remove_camera(slot)
        self.state.save()
        self.refresh()
        self.notify()

    def _refresh_slots(self) -> None:
        for slot, label in self.slot_labels.items():
            cam = self.state.stand.camera_at(slot)
            self.remove_buttons[slot].setVisible(cam is not None)
            if cam is None:
                label.setText("свободно")
                continue
            label.setText(f"{cam.title or 'марка не указана'}\n{cam.address}:{cam.port}")

    def refresh(self) -> None:
        self._refresh_slots()
        self._sync_rows()
        self._fill_table()
