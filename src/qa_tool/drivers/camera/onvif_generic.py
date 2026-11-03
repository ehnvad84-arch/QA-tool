"""Общий ONVIF-драйвер: годится для любой камеры, своей и конкурента (AD-4)."""

from dataclasses import dataclass

from qa_tool.core.models import Camera
from qa_tool.drivers.camera.soap import NS, SoapClient


@dataclass(frozen=True)
class Passport:
    manufacturer: str
    model: str
    firmware: str
    serial: str
    hardware: str


class OnvifCamera:
    def __init__(self, camera: Camera, password: str, timeout: float = 5.0):
        self.camera = camera
        self.soap = SoapClient(camera.username, password, timeout)

    @property
    def device_url(self) -> str:
        return f"http://{self.camera.address}:{self.camera.port}/onvif/device_service"

    @property
    def exchanges(self):
        return self.soap.exchanges

    def get_device_information(self) -> Passport:
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
