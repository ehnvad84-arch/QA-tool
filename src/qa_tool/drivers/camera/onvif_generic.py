"""Общий ONVIF-драйвер: годится для любой камеры, своей и конкурента (AD-4)."""

from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from qa_tool.core.models import Camera
from qa_tool.drivers.camera.soap import NS, OnvifFault, SoapClient


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
        if urlsplit(url).hostname == camera.address:
            out[key] = url
        elif foreign is not None:
            foreign.append(key)
    return out


@dataclass(frozen=True)
class EncoderSettings:
    """Что камера заявляет о потоке; None — поля нет в ответе.

    `kbps` — `BitrateLimit`: при постоянном битрейте цель, при переменном потолок.
    `constant` — постоянный ли битрейт; None — камера не сказала (Media1 этого не сообщает).
    """

    encoding: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    kbps: float | None = None
    constant: bool | None = None
    gop: int | None = None


@dataclass(frozen=True)
class MediaProfile:
    """Медиапрофиль: адрес потока и заявленные настройки.

    `encoder` None — настроек нет ни в Media2, ни в Media1. `problem` — почему нет адреса.
    """

    token: str
    name: str
    uri: str
    encoder: EncoderSettings | None
    problem: str = ""


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _number(element: Element, name: str) -> float | None:
    """Первое число с таким именем внутри `element`: атрибут или вложенный элемент."""
    for item in element.iter():
        text = item.get(name) if name in item.attrib else None
        if text is None and _local(item.tag) == name:
            text = item.text
        if text is None:
            continue
        try:
            return float(text.strip())
        except ValueError:
            return None
    return None


def parse_encoder(config: Element | None) -> EncoderSettings | None:
    """Настройки из VideoEncoder (Media2) или VideoEncoderConfiguration (Media1).

    Returns:
        Настройки; None — настроек нет или кодек не указан: так Dahua отдаёт в Media1 поток H.265.
    """
    if config is None:
        return None
    encoding = next(
        ((e.text or "").strip() for e in config.iter() if _local(e.tag) == "Encoding"), ""
    ) or config.get("Encoding", "")
    if not encoding:
        return None
    rate = next((e for e in config.iter() if _local(e.tag) == "RateControl"), None)
    fps = kbps = constant = None
    if rate is not None:
        fps = _number(rate, "FrameRateLimit")
        interval = _number(rate, "EncodingInterval")
        fps = fps / interval if fps and interval and interval > 1 else fps
        kbps = _number(rate, "BitrateLimit")
        raw = rate.get("ConstantBitRate", "").strip().lower()
        constant = raw in ("true", "1") if raw else None
    width, height, gop = (
        _number(config, "Width"),
        _number(config, "Height"),
        _number(config, "GovLength"),
    )
    return EncoderSettings(
        encoding.upper().replace("HEVC", "H265"),
        int(width) if width else None,
        int(height) if height else None,
        fps or None,
        kbps or None,
        constant,
        int(gop) if gop else None,
    )


def _profiles(soap: SoapClient, url: str, media2: bool) -> list[tuple[str, str, Element | None]]:
    """Токен, имя и настройки кодировщика каждого профиля одной службы."""
    body = "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>"
    answer = soap.call(url, body if media2 else "<trt:GetProfiles/>")
    found = []
    for profile in (e for e in answer.iter() if _local(e.tag) == "Profiles"):
        name = next((c.text or "" for c in profile if _local(c.tag) == "Name"), "")
        config = next(
            (
                e
                for e in profile.iter()
                if _local(e.tag) in ("VideoEncoder", "VideoEncoderConfiguration")
            ),
            None,
        )
        found.append((profile.get("token", ""), name.strip(), config))
    return found


def stream_uri(soap: SoapClient, url: str, media2: bool, token: str) -> str:
    """Адрес RTSP профиля; пусто — камера его не сообщила.

    Raises:
        CameraUnreachable, OnvifFault: GetStreamUri не прошёл.
    """
    prefix = "tr2" if media2 else "trt"
    setup = (
        "<tr2:Protocol>RTSP</tr2:Protocol>"
        if media2
        else "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
        "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
    )
    answer = soap.call(
        url,
        f"<{prefix}:GetStreamUri>{setup}"
        f"<{prefix}:ProfileToken>{escape(token)}</{prefix}:ProfileToken>"
        f"</{prefix}:GetStreamUri>",
    )
    return next(((e.text or "").strip() for e in answer.iter() if _local(e.tag) == "Uri"), "")


def _read_service(soap: SoapClient, services: dict[str, str], key: str) -> list:
    if key not in services:
        return []
    try:
        return _profiles(soap, services[key], key == "media2")
    except OnvifFault:
        return []


def media_profiles(soap: SoapClient, services: dict[str, str]) -> list[MediaProfile]:
    """Все медиапрофили с адресами и настройками: Media2, а чего там нет — из Media1.

    Настройки берутся из той службы, где они есть: у Dahua поток H.265 виден только в Media2,
    а старые камеры Media2 не объявляют вовсе.

    Raises:
        CameraUnreachable: камера не ответила.
        OnvifFault: нет службы медиа или ни одного профиля.
    """
    if "media2" not in services and "media" not in services:
        raise OnvifFault("камера не объявляет службу медиа (Media или Media2)")
    media2 = _read_service(soap, services, "media2")
    needs_media1 = not media2 or any(parse_encoder(c) is None for _, _, c in media2)
    media1 = _read_service(soap, services, "media") if needs_media1 else []
    primary, is_media2 = (media2, True) if media2 else (media1, False)
    if not primary:
        raise OnvifFault("GetProfiles не вернул ни одного медиапрофиля")
    url = services["media2" if is_media2 else "media"]
    spare = {token: config for token, _, config in media1}
    profiles = []
    for token, name, config in primary:
        encoder = parse_encoder(config) or parse_encoder(spare.get(token))
        problem = ""
        try:
            uri = stream_uri(soap, url, is_media2, token)
        except OnvifFault as e:
            uri, problem = "", f"GetStreamUri: {e}"
        profiles.append(MediaProfile(token, name, uri, encoder, problem))
    return profiles


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
