"""Виртуальная ONVIF-камера для тестов и ручной проверки окна.

Умеет: SOAP GetDeviceInformation и GetCapabilities с проверкой WS-Security UsernameToken
(PasswordDigest), RTSP OPTIONS на отдельном порту, ответ на WS-Discovery Probe — один
ответчик на весь «парк» камер. Запуск для человека:

    uv run python -m tests.mock_camera --count 9 --write-stand stand.yaml
"""

import argparse
import base64
import datetime
import hashlib
import socket
import struct
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.mock_onvif import ENVELOPE_NS, IMAGING_FULL, OnvifServices, OnvifState
from tests.mock_rtsp import MockRtspServer, imaging_video

WSD_GROUP, WSD_PORT = "239.255.255.250", 3702
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
}
PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
    "#PasswordDigest"
)
VENDORS = ["Ruver", "Hikvision", "Dahua", "Axis", "Uniview", "Raysharp"]


@dataclass
class Passport:
    manufacturer: str
    model: str
    firmware: str
    serial: str
    hardware: str = "HW-1"


def _envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" {ENVELOPE_NS}>'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def _fault(reason: str, subcode: str = "ter:NotAuthorized", detail: str = "") -> bytes:
    return _envelope(
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
        f"<s:Value>{subcode}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason>'
        f"{f'<s:Detail>{detail}</s:Detail>' if detail else ''}</s:Fault>"
    )


def password_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()


class MockCamera:
    auth_reply = "onvif"  # отказ по паролю: onvif, gsoap (TMID-018), http401, http401page, soap11
    clock_skew_s = 0  # часы камеры ушли: отметка времени запроса считается просроченной

    def __init__(
        self,
        passport: Passport,
        username: str = "admin",
        password: str = "admin",
        state: OnvifState | None = None,
        web_pages: dict[str, str] | None = None,
    ):
        self.passport = passport
        self.web_pages = web_pages or {}  # веб-страницы камеры: проверка сборки с браузером
        self.state = state or OnvifState()
        self.username = username
        self.password = password
        self.requests: list[str] = []
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.rtsp = MockRtspServer(
            metadata="M" in self.state.profiles and self.state.metadata_stream,
            require_unsupported=self.state.replay_require_unsupported,
            udp=self.state.rtsp_udp,
            udp_blackhole=self.state.rtsp_udp_blackhole,
            range_silent=self.state.replay_range_silent,
            picture=self._picture if self.state.imaging_video else None,
        )
        self._threads: list[threading.Thread] = []

    def _picture(self) -> bytes | None:
        return imaging_video(self.state.picture())

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    @property
    def rtsp_port(self) -> int:
        return self.rtsp.port

    @property
    def xaddr(self) -> str:
        return f"http://127.0.0.1:{self.port}/onvif/device_service"

    def start(self) -> "MockCamera":
        t = threading.Thread(target=self._http.serve_forever, args=(0.05,), daemon=True)
        t.start()
        self._threads.append(t)
        self.rtsp.start()
        return self

    def stop(self) -> None:
        """Камера «пропала из сети»: оба порта закрываются. Повторный вызов ничего не делает."""
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        self._http.shutdown()
        self._http.server_close()
        self.rtsp.stop()

    def _authorized(self, root: ET.Element) -> bool:
        token = root.find(".//wsse:UsernameToken", NS)
        if token is None:
            return False
        user = token.findtext("wsse:Username", default="", namespaces=NS)
        password = token.find("wsse:Password", NS)
        # как gSOAP: тип сравнивается точно, иначе пароль считается открытым текстом и не совпадает
        if password is None or password.get("Type") != PASSWORD_DIGEST_TYPE:
            return False
        digest = password.text or ""
        nonce = base64.b64decode(token.findtext("wsse:Nonce", default="", namespaces=NS))
        created = token.findtext("wsu:Created", default="", namespaces=NS)
        if self.clock_skew_s:
            sent = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            if abs((sent - self._now()).total_seconds()) > 5:
                return False
        return user == self.username and digest == password_digest(nonce, created, self.password)

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=self.clock_skew_s)

    def answer(self, payload: bytes) -> tuple[int, bytes]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as e:  # как настоящая камера: ошибка в ответ, а не обрыв связи
            return 400, _fault(f"Malformed request: {e}")
        body = root.find("s:Body", NS)
        operation = body[0].tag.split("}")[-1] if body is not None and len(body) else ""
        self.requests.append(operation)
        if operation == "GetSystemDateAndTime" and self.clock_skew_s:
            t = self._now()
            return 200, _envelope(
                "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime><tt:Time>"
                f"<tt:Hour>{t.hour}</tt:Hour><tt:Minute>{t.minute}</tt:Minute>"
                f"<tt:Second>{t.second}</tt:Second></tt:Time><tt:Date><tt:Year>{t.year}</tt:Year>"
                f"<tt:Month>{t.month}</tt:Month><tt:Day>{t.day}</tt:Day></tt:Date></tt:UTCDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
            )
        if not self._authorized(root):
            if self.auth_reply == "gsoap":
                reason = "The security token could not be authenticated or authorized"
                return 400, _fault(reason, "wsse:FailedAuthentication")
            if self.auth_reply == "http401":
                return 401, b"Unauthorized"
            if self.auth_reply == "http401page":
                page = b"<html><head><title>401</title></head><body>Unauthorized</body></html>"
                return 401, page
            if self.auth_reply == "soap11":
                return 500, (
                    b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/"><e:Body>'
                    b"<e:Fault><faultcode>ter:NotAuthorized</faultcode>"
                    b"<faultstring>denied</faultstring></e:Fault></e:Body></e:Envelope>"
                )
            return 400, _fault("Sender not Authorized")
        p = self.passport
        if operation == "GetDeviceInformation":
            return 200, _envelope(
                "<tds:GetDeviceInformationResponse>"
                f"<tds:Manufacturer>{p.manufacturer}</tds:Manufacturer><tds:Model>{p.model}</tds:Model>"
                f"<tds:FirmwareVersion>{p.firmware}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{p.serial}</tds:SerialNumber>"
                f"<tds:HardwareId>{p.hardware}</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>"
            )
        if operation == "GetCapabilities":
            base = f"http://127.0.0.1:{self.port}/onvif"
            return 200, _envelope(
                "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                f"<tt:Device><tt:XAddr>{base}/device_service</tt:XAddr></tt:Device>"
                f"<tt:Media><tt:XAddr>{base}/media_service</tt:XAddr></tt:Media>"
                "</tds:Capabilities></tds:GetCapabilitiesResponse>"
            )
        services = OnvifServices(
            self.state, f"http://127.0.0.1:{self.port}/onvif", f"rtsp://127.0.0.1:{self.rtsp_port}"
        )
        services.envelope = root
        answer = services.handle(operation, body[0])
        if answer is not None:
            return 200, _envelope(answer)
        if services.fault:
            return 500, _fault(services.fault, services.fault, services.fault_detail)
        return 400, _fault(f"Action not supported: {operation}", "ter:ActionNotSupported")

    def _handler(self):
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — имя задаёт http.server
                page = camera.web_pages.get(self.path.split("?")[0])
                if page is None:
                    self.send_error(404)
                    return
                data = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802 — имя задаёт http.server
                payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                code, response = camera.answer(payload)
                self.send_response(code)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        return Handler


class DiscoveryResponder:
    """Отвечает на WS-Discovery Probe за все камеры парка."""

    def __init__(self, cameras: list[MockCamera], interface: str = "127.0.0.1"):
        self.cameras = cameras
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", WSD_PORT))
        membership = struct.pack("4s4s", socket.inet_aton(WSD_GROUP), socket.inet_aton(interface))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "DiscoveryResponder":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            if b"Probe" not in data or b"ProbeMatch" in data:
                continue
            message_id = ET.fromstring(data).findtext(".//wsa:MessageID", default="", namespaces=NS)
            for camera in list(self.cameras):
                self.sock.sendto(self._probe_match(camera, message_id), sender)

    @staticmethod
    def _probe_match(camera: MockCamera, relates_to: str) -> bytes:
        p = camera.passport
        scopes = (
            f"onvif://www.onvif.org/name/{p.manufacturer} "
            f"onvif://www.onvif.org/hardware/{p.model} onvif://www.onvif.org/Profile/Streaming"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:a="{NS["wsa"]}"'
            ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
            ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            "<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches"
            f"</a:Action><a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
            f"<a:RelatesTo>{relates_to}</a:RelatesTo></s:Header>"
            "<s:Body><d:ProbeMatches><d:ProbeMatch>"
            f"<a:EndpointReference><a:Address>urn:uuid:{p.serial}</a:Address></a:EndpointReference>"
            f"<d:Types>dn:NetworkVideoTransmitter</d:Types><d:Scopes>{scopes}</d:Scopes>"
            f"<d:XAddrs>{camera.xaddr}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>"
            "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
        ).encode()


def start_fleet(count: int, with_discovery: bool = False, imaging: bool = False):
    """Запустить `count` виртуальных камер; `imaging` — с полными настройками картинки и видео,
    которое от них зависит (проверка картинки, этап 7)."""
    cameras = [
        MockCamera(
            Passport(
                manufacturer=VENDORS[i % len(VENDORS)],
                model=f"MOCK-{i + 1}",
                firmware=f"0.{i + 1}.0",
                serial=f"SN{i + 1:04d}",
            ),
            state=OnvifState(imaging_xml=IMAGING_FULL, imaging_video=True) if imaging else None,
        ).start()
        for i in range(count)
    ]
    responder = DiscoveryResponder(cameras).start() if with_discovery else None
    return cameras, responder


def stand_yaml(cameras: list[MockCamera], place: str = "color_box") -> str:
    lines = [f"place: {place}", "runs_dir: runs", "cameras:"]
    for slot, cam in enumerate(cameras[:8], start=1):
        vendor = cam.passport.manufacturer.lower()
        lines.append(
            f"  - {{slot: {slot}, address: 127.0.0.1, port: {cam.port}, "
            f"rtsp_port: {cam.rtsp_port}, username: admin, vendor: {vendor}}}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Виртуальные ONVIF-камеры (логин и пароль admin)")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--write-stand", type=Path, help="записать stand.yaml для первых 8 камер")
    args = parser.parse_args()
    cameras, responder = start_fleet(args.count, with_discovery=True, imaging=True)
    for cam in cameras:
        p = cam.passport
        print(f"{p.manufacturer:<10} {p.model:<8} {cam.xaddr}  rtsp:{cam.rtsp_port}")
    if args.write_stand:
        args.write_stand.write_text(stand_yaml(cameras), encoding="utf-8")
        print(f"stand.yaml записан: {args.write_stand}")
    print("Логин admin, пароль admin. Остановить — Ctrl+C.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        responder.stop()
        for cam in cameras:
            cam.stop()


if __name__ == "__main__":
    main()
