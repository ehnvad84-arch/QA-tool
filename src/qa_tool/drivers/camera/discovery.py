"""Поиск камер WS-Discovery: Probe в группу 239.255.255.250:3702 или напрямую на адрес."""

import socket
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from defusedxml import ElementTree

WSD_GROUP, WSD_PORT = "239.255.255.250", 3702
_NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}


@dataclass
class FoundCamera:
    """Камера, ответившая на WS-Discovery Probe."""

    address: str
    port: int
    xaddrs: list[str]
    manufacturer: str = ""
    model: str = ""
    scopes: list[str] = field(default_factory=list)


def _probe() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header>'
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>"
        "<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>"
        "</s:Envelope>"
    ).encode()


def _scope_value(scopes: list[str], kind: str) -> str:
    prefix = f"onvif://www.onvif.org/{kind}/"
    for scope in scopes:
        if scope.startswith(prefix):
            return unquote(scope[len(prefix) :])
    return ""


def parse_probe_matches(data: bytes) -> list[FoundCamera]:
    """Разобрать ответ ProbeMatches; устройства с битым адресом пропускаются.

    Raises:
        ParseError, ValueError: пакет не XML или с DTD/ENTITY (defusedxml) —
            `discover()` такие пакеты пропускает.
    """
    root = ElementTree.fromstring(data)
    found = []
    for match in root.iterfind(".//d:ProbeMatch", _NS):
        xaddrs = (match.findtext("d:XAddrs", default="", namespaces=_NS) or "").split()
        if not xaddrs:
            continue
        url = urlparse(xaddrs[0])
        try:
            port = url.port or 80
        except ValueError:  # «:99999» и прочий мусор от чужого устройства — пропускаем
            continue
        if not url.hostname:
            continue
        scopes = (match.findtext("d:Scopes", default="", namespaces=_NS) or "").split()
        found.append(
            FoundCamera(
                address=url.hostname,
                port=port,
                xaddrs=xaddrs,
                manufacturer=_scope_value(scopes, "name"),
                model=_scope_value(scopes, "hardware"),
                scopes=scopes,
            )
        )
    return found


def discover(
    timeout: float = 3.0, target: str | None = None, interface: str = ""
) -> list[FoundCamera]:
    """Камеры, ответившие на Probe за `timeout` секунд.

    `target` — адрес для прямого Probe (камера в другой подсети); `interface` — IP сетевой
    карты компьютера, смотрящей в сеть камер (пусто — карта по умолчанию).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)  # только своя подсеть
    if interface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.bind(("", 0))
    found: dict[str, FoundCamera] = {}
    try:
        sock.sendto(_probe(), (target or WSD_GROUP, WSD_PORT))
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, _ = sock.recvfrom(65535)
            except TimeoutError:
                break
            try:
                matches = parse_probe_matches(data)
            except (ElementTree.ParseError, ValueError):  # битый XML, DTD, ENTITY (defusedxml)
                continue  # чужой или битый пакет — пропускаем
            for camera in matches:
                found.setdefault(camera.xaddrs[0], camera)
    finally:
        sock.close()
    return sorted(found.values(), key=lambda c: (c.address, c.port))
