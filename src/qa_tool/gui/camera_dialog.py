"""Окно «Камера на место»: адрес, порты, логин, пароль, производитель, место."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
)

from qa_tool.core.models import RUVER, Camera
from qa_tool.gui.help import helped

VENDORS = [
    RUVER,
    "hikvision",
    "dahua",
    "tiandy",
    "beward",
    "axis",
    "uniview",
    "raysharp",
    "generic",
]


def vendor_from_manufacturer(manufacturer: str) -> str:
    word = (manufacturer.split() or ["generic"])[0].lower()
    return word if word in VENDORS else "generic"


class CameraDialog(QDialog):
    def __init__(self, free_slots: list[int], address="", port=80, vendor="generic", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Камера на место стенда")
        self.setProperty("help_id", "cameras-add")
        form = QFormLayout(self)
        self.address = helped(
            QLineEdit(address),
            "IP-адрес камеры в сети стенда, например 192.168.1.10.",
            "cameras-add",
        )
        self.port = helped(QSpinBox(), "HTTP-порт камеры (ONVIF и веб). Обычно 80.", "cameras-add")
        self.port.setRange(1, 65535)
        self.port.setValue(port)
        self.rtsp_port = helped(QSpinBox(), "RTSP-порт видеопотока. Обычно 554.", "cameras-add")
        self.rtsp_port.setRange(1, 65535)
        self.rtsp_port.setValue(554)
        self.lan_address = helped(
            QLineEdit(),
            "Заполняется, только если стенд ходит к камере через туннель или NAT: адрес камеры "
            "в её собственной сети, например 192.168.1.10. Для камеры в сети стенда — пусто.",
            "cameras-add",
        )
        self.username = helped(QLineEdit("admin"), "Логин администратора камеры.", "cameras-add")
        self.password = helped(
            QLineEdit(),
            "Пароль камеры. Хранится в системном хранилище паролей, "
            "в файлы стенда и протоколы не попадает.",
            "cameras-add",
        )
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.vendor = helped(
            QComboBox(),
            "Производитель. Любая камера проверяется по ONVIF, потоку, картинке и "
            "веб-интерфейсу; если для производителя есть адаптер, добавляются проверки его "
            "API, а у Hikvision, Dahua и «Рувер» снимается карта родного API. Производитель "
            "из паспорта ONVIF главнее этого поля. Нет в списке — «generic».",
            "cameras-add",
        )
        self.vendor.addItems(VENDORS)
        self.vendor.setCurrentText(vendor)
        self.slot = helped(
            QComboBox(), "Место на стенде (1–8), куда поставлена камера.", "cameras-add"
        )
        for slot in free_slots:
            self.slot.addItem(str(slot), slot)
        for label, widget in [
            ("Адрес", self.address),
            ("HTTP-порт", self.port),
            ("RTSP-порт", self.rtsp_port),
            ("Адрес за туннелем", self.lan_address),
            ("Логин", self.username),
            ("Пароль", self.password),
            ("Производитель", self.vendor),
            ("Место", self.slot),
        ]:
            form.addRow(label, widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        for button in buttons.buttons():
            helped(button, "Сохранить камеру на выбранном месте или отменить.", "cameras-add")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def camera(self) -> Camera:
        return Camera(
            slot=int(self.slot.currentData()),
            address=self.address.text().strip(),
            port=self.port.value(),
            rtsp_port=self.rtsp_port.value(),
            username=self.username.text().strip(),
            vendor=self.vendor.currentText(),
            lan_address=self.lan_address.text().strip(),
        )
