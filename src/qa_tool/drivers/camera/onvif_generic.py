"""Общий ONVIF-драйвер: годится для любой камеры, своей и конкурента (AD-4)."""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from qa_tool.core.models import Camera
from qa_tool.drivers.camera.soap import NS, SoapClient


@dataclass(frozen=True)
class Passport:
    """Паспорт камеры из ONVIF GetDeviceInformation."""

    manufacturer: str
    model: str
    firmware: str
    serial: str
    hardware: str


# пространство имён службы ONVIF → короткое имя (GetServices)
SERVICE_KEYS = {
    "http://www.onvif.org/ver10/device/wsdl": "device",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
}


def stand_service_url(camera: Camera, url: str) -> str | None:
    """Адрес службы, по которому до неё дойдёт стенд; None — служба на чужом хосте.

    Камера за туннелем называет службы своим внутренним адресом, а Dahua — хостом из запроса,
    но без порта. Оба варианта ведут на проброшенный порт: без этого запрос с логином ушёл бы
    на порт 80 компьютера стенда.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # адрес от камеры, разобрать нельзя: на такой идти нельзя
        return None
    aliases = (camera.address.lower(), camera.lan_address.lower())
    if camera.lan_address and host in aliases:
        return urlunsplit(parts._replace(scheme="http", netloc=f"{camera.address}:{camera.port}"))
    return url if host == camera.address.lower() else None


def service_urls(
    soap: SoapClient, camera: Camera, foreign: list[str] | None = None
) -> dict[str, str]:
    """Адреса служб камеры по GetServices; адрес на чужом хосте отброшен (security.md).

    Args:
        soap: клиент ONVIF камеры.
        camera: камера со стенда.
        foreign: сюда дописываются имена отброшенных служб — для причины в протоколе.

    Raises:
        CameraUnreachable, OnvifFault: GetServices не прошёл.
    """
    device_url = f"http://{camera.address}:{camera.port}/onvif/device_service"
    answer = soap.call(
        device_url,
        "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>",
    )
    out = {"device": device_url}
    for service in (e for e in answer.iter() if e.tag.endswith("}Service")):
        namespace = next((c.text or "" for c in service if c.tag.endswith("}Namespace")), "")
        url = next((c.text or "" for c in service if c.tag.endswith("}XAddr")), "").strip()
        key = SERVICE_KEYS.get(namespace.strip())
        if not key or key == "device":
            continue
        reachable = stand_service_url(camera, url)
        if reachable:
            out[key] = reachable
        elif foreign is not None:
            foreign.append(key)
    return out


class OnvifCamera:
    """Общий ONVIF-драйвер для камеры любого производителя.

    Каждый обмен сохраняется в `exchanges` с вырезанными секретами — это
    доказательства для протокола.
    """

    def __init__(self, camera: Camera, password: str, timeout: float = 5.0):
        self.camera = camera
        self.soap = SoapClient(camera.username, password, timeout)

    @property
    def device_url(self) -> str:
        """Адрес Device Service (стандартный путь ONVIF)."""
        return f"http://{self.camera.address}:{self.camera.port}/onvif/device_service"

    @property
    def exchanges(self):
        """Обмены с камерой с вырезанными секретами."""
        return self.soap.exchanges

    def get_device_information(self) -> Passport:
        """Паспорт камеры.

        Raises:
            CameraUnreachable: камера не ответила.
            OnvifFault: камера ответила SOAP Fault (например, NotAuthorized).
        """
        r = self.soap.call(self.device_url, "<tds:GetDeviceInformation/>")

        def text(tag: str) -> str:
            return (r.findtext(f"tds:{tag}", default="", namespaces=NS) or "").strip()

        return Passport(
            manufacturer=text("Manufacturer"),
            model=text("Model"),
            firmware=text("FirmwareVersion"),
            serial=text("SerialNumber"),
            hardware=text("HardwareId"),
        )

    def get_capabilities(self) -> dict[str, str]:
        """Сервис → адрес: {'Device': 'http://…', 'Media': 'http://…', …}."""
        r = self.soap.call(
            self.device_url,
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
        )
        caps = r.find("tds:Capabilities", NS)
        services = {}
        for service in caps if caps is not None else []:
            xaddr = service.findtext("tt:XAddr", default="", namespaces=NS)
            if xaddr:
                services[service.tag.split("}")[-1]] = xaddr
        return services
