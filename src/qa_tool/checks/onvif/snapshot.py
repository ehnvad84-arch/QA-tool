"""Снимок и восстановление настроек камеры вокруг изменяющих тестов onvif-tt (AD-3, never.md).

Изменяющие тесты закреплённой версии onvif-tt (`requires_writes`) меняют: дату и время
(DEVICE-3-1-11 ставит режим Manual), тревожные выходы (DEVICEIO-1-2-1), фокус (IMAGING-2-1-*),
положение и настройки PTZ (PTZ-2-1-9/10, PTZ-3-1-*, PTZ-7-3-*). Перед прогоном эти настройки
читаются, после — возвращаются и сверяются. Сеть и учётные записи здесь не трогаются: их тесты
не меняют, а восстановление шло бы через запрещённые операции (AD-7).

Всё идёт через `SoapClient`, то есть через табу. Адреса служб берутся из GetServices; адрес
на чужом хосте не используется (security.md: только адреса со стенда).
"""

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, tostring
from xml.sax.saxutils import escape

from qa_tool.core.models import Camera
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

POSITION_TOLERANCE = 0.02  # шаг привода у PTZ и фокуса: точнее камеры возвращаются редко


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find_all(element: Element, name: str) -> list[Element]:
    return [e for e in element.iter() if _local(e.tag) == name]


def _text(element: Element, name: str, default: str = "") -> str:
    found = _find_all(element, name)
    return (found[0].text or "").strip() if found else default


def canonical(element: Element) -> list[tuple]:
    """Содержимое элемента без префиксов и пробелов — для сравнения «было / стало»."""
    return [
        (_local(e.tag), (e.text or "").strip(), tuple(sorted(e.attrib.items())))
        for e in element.iter()
    ]


def _inner(element: Element) -> str:
    return "".join(tostring(child, encoding="unicode") for child in element)


@dataclass
class Item:
    """Одна сохранённая настройка: как вернуть и как сверить."""

    area: str
    restore: list[tuple[str, str]]  # (адрес службы, тело запроса) — по порядку
    read: tuple[str, str]  # (адрес службы, тело запроса) для сверки
    expected: object  # сравнимое значение до прогона
    compare: str = "canonical"  # способ сверки: canonical, datetime или position


@dataclass
class Snapshot:
    """Сохранённые настройки камеры и то, что сохранить не удалось."""

    items: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class SnapshotTaker:
    """Снимок настроек камеры перед изменяющими тестами и их восстановление после.

    Args:
        camera: камера со стенда (адрес — для отсева чужих хостов в XAddr).
        soap: клиент ONVIF камеры (табу внутри).
    """

    def __init__(self, camera: Camera, soap: SoapClient):
        self.camera = camera
        self.soap = soap
        self.device_url = f"http://{camera.address}:{camera.port}/onvif/device_service"

    def services(self) -> dict[str, str]:
        """Адреса служб камеры по GetServices; адреса на чужих хостах отброшены."""
        return service_urls(self.soap, self.camera)

    # --- снимок -------------------------------------------------------------------------------

    def take(self) -> Snapshot:
        """Прочитать все настройки, которые меняют изменяющие тесты onvif-tt.

        Область, которую прочитать не удалось, попадает в `skipped` с причиной: её
        восстановить будет нельзя, и вызывающий решает, запускать ли изменяющие тесты.
        """
        snap = Snapshot()
        services = self.services()
        for area, take in (
            ("дата и время", self._datetime),
            ("тревожные выходы", self._relays),
            ("изображение и фокус", self._imaging),
            ("PTZ", self._ptz),
        ):
            try:
                snap.items += take(services)
            except (CameraUnreachable, OnvifFault, ValueError) as e:
                snap.skipped.append(f"{area}: {e}")
        return snap

    def _datetime(self, services) -> list[Item]:
        url = services["device"]
        answer = self.soap.call(url, "<tds:GetSystemDateAndTime/>")
        mode = _text(answer, "DateTimeType", "NTP")
        dst = _text(answer, "DaylightSavings", "false")
        tz = _text(answer, "TZ")
        zone = f"<tds:TimeZone><tt:TZ>{escape(tz)}</tt:TZ></tds:TimeZone>" if tz else ""
        # В режиме Manual камера хранит своё время: вернуть «как было» нельзя — время идёт;
        # ставим текущее время UTC компьютера стенда, режим и часовой пояс — исходные.
        utc = ""
        if mode == "Manual":
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            utc = (
                "<tds:UTCDateTime><tt:Date>"
                f"<tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
                f"<tt:Day>{now.day}</tt:Day></tt:Date><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>"
                "</tt:Time></tds:UTCDateTime>"
            )
        body = (
            f"<tds:SetSystemDateAndTime><tds:DateTimeType>{escape(mode)}</tds:DateTimeType>"
            f"<tds:DaylightSavings>{escape(dst)}</tds:DaylightSavings>{zone}{utc}"
            "</tds:SetSystemDateAndTime>"
        )
        return [
            Item(
                "дата и время",
                [(url, body)],
                (url, "<tds:GetSystemDateAndTime/>"),
                (mode, dst, tz),
                "datetime",
            )
        ]

    def _relays(self, services) -> list[Item]:
        answer = self.soap.call(services["device"], "<tds:GetRelayOutputs/>")
        items = []
        for relay in _find_all(answer, "RelayOutputs"):
            token = relay.get("token", "")
            properties = next((e for e in relay if _local(e.tag) == "Properties"), None)
            if not token or properties is None:
                continue
            safe = escape(token)
            restore = [
                (
                    services["device"],
                    f"<tds:SetRelayOutputSettings><tds:RelayOutputToken>{safe}"
                    f"</tds:RelayOutputToken><tds:Properties>{_inner(properties)}</tds:Properties>"
                    "</tds:SetRelayOutputSettings>",
                ),
                (
                    services["device"],
                    f"<tds:SetRelayOutputState><tds:RelayOutputToken>{safe}</tds:RelayOutputToken>"
                    "<tds:LogicalState>inactive</tds:LogicalState></tds:SetRelayOutputState>",
                ),
            ]
            items.append(
                Item(
                    f"тревожный выход {token}",
                    restore,
                    (services["device"], "<tds:GetRelayOutputs/>"),
                    canonical(relay),
                )
            )
        return items

    def _video_sources(self, services) -> list[str]:
        answer = self.soap.call(services.get("media", services["device"]), "<trt:GetVideoSources/>")
        return [e.get("token", "") for e in _find_all(answer, "VideoSources") if e.get("token")]

    def _imaging(self, services) -> list[Item]:
        if "imaging" not in services:
            return []
        url = services["imaging"]
        items = []
        for token in self._video_sources(services):
            source = f"<timg:VideoSourceToken>{escape(token)}</timg:VideoSourceToken>"
            read = (
                url,
                f"<timg:GetImagingSettings>{source}</timg:GetImagingSettings>",
            )
            settings = _find_all(self.soap.call(*read), "ImagingSettings")[0]
            restore = [
                (
                    url,
                    f"<timg:SetImagingSettings>{source}"
                    f"<timg:ImagingSettings>{_inner(settings)}</timg:ImagingSettings>"
                    "<timg:ForcePersistence>true</timg:ForcePersistence></timg:SetImagingSettings>",
                )
            ]
            if _text(settings, "AutoFocusMode") == "MANUAL":
                status = self.soap.call(
                    url,
                    f"<timg:GetStatus>{source}</timg:GetStatus>",
                )
                position = _text(status, "Position")
                if position:
                    restore.append(
                        (
                            url,
                            f"<timg:Move>{source}"
                            "<timg:Focus><tt:Absolute><tt:Position>"
                            f"{escape(position)}</tt:Position></tt:Absolute></timg:Focus></timg:Move>",
                        )
                    )
            items.append(Item(f"изображение и фокус {token}", restore, read, canonical(settings)))
        return items

    def _ptz(self, services) -> list[Item]:
        if "ptz" not in services:
            return []
        url = services["ptz"]
        items = []
        answer = self.soap.call(url, "<tptz:GetConfigurations/>")
        for config in _find_all(answer, "PTZConfiguration"):
            token = config.get("token", "")
            items.append(
                Item(
                    f"настройки PTZ {token}",
                    [
                        (
                            url,
                            f"<tptz:SetConfiguration>{tostring(config, encoding='unicode')}"
                            "<tptz:ForcePersistence>true</tptz:ForcePersistence>"
                            "</tptz:SetConfiguration>",
                        )
                    ],
                    (url, "<tptz:GetConfigurations/>"),
                    canonical(config),
                )
            )
        media2 = services.get("media2")
        profiles = (
            _find_all(self.soap.call(media2, "<tr2:GetProfiles/>"), "Profiles") if media2 else []
        )
        for profile in profiles:
            token = profile.get("token", "")
            safe = escape(token)
            read = (
                url,
                f"<tptz:GetStatus><tptz:ProfileToken>{safe}</tptz:ProfileToken></tptz:GetStatus>",
            )
            positions = _find_all(self.soap.call(*read), "Position")
            if not positions:
                continue
            body = (
                f"<tptz:AbsoluteMove><tptz:ProfileToken>{safe}</tptz:ProfileToken>"
                f"<tptz:Position>{_inner(positions[0])}</tptz:Position></tptz:AbsoluteMove>"
            )
            items.append(
                Item(
                    f"положение PTZ {token}",
                    [(url, body)],
                    read,
                    _position(positions[0]),
                    "position",
                )
            )
        return items

    # --- восстановление -----------------------------------------------------------------------

    def restore(self, snap: Snapshot) -> tuple[list[str], list[str]]:
        """Вернуть сохранённые настройки и сверить их.

        Returns:
            (что вернулось, что осталось изменённым — с причиной).
        """
        restored, problems = [], []
        for item in snap.items:
            try:
                for url, body in item.restore:
                    self.soap.call(url, body)
                actual = self._read_back(item)
            except (CameraUnreachable, OnvifFault, ValueError) as e:
                problems.append(f"{item.area}: {e}")
                continue
            if _same(item, actual):
                restored.append(item.area)
            else:
                problems.append(f"{item.area}: после восстановления не совпадает с исходным")
        return restored, problems

    def _read_back(self, item: Item):
        answer = self.soap.call(*item.read)
        if item.compare == "datetime":
            return (
                _text(answer, "DateTimeType", "NTP"),
                _text(answer, "DaylightSavings", "false"),
                _text(answer, "TZ"),
            )
        if item.compare == "position":
            positions = _find_all(answer, "Position")
            return _position(positions[0]) if positions else None
        tag = {"тревожный": "RelayOutputs", "изображение": "ImagingSettings"}.get(
            item.area.split()[0], "PTZConfiguration"
        )
        token = item.area.split()[-1]
        for element in _find_all(answer, tag):
            if tag == "ImagingSettings" or element.get("token") == token:
                return canonical(element)
        return None


def _position(element: Element) -> list[float]:
    return [float(v) for e in element.iter() for k, v in e.attrib.items() if k in ("x", "y")]


def _same(item: Item, actual) -> bool:
    if actual is None:
        return False
    if item.compare == "position":
        return len(actual) == len(item.expected) and all(
            abs(a - b) <= POSITION_TOLERANCE for a, b in zip(actual, item.expected, strict=True)
        )
    return actual == item.expected
