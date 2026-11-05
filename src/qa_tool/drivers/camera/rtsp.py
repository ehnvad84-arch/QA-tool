"""Минимальный RTSP-клиент для чтения потоков камеры: видео (модуль «Поток»), метаданные ONVIF
(Profile M) и архив Profile G.

Зачем свой клиент, а не ffmpeg: ffmpeg не разбирает дорожку `vnd.onvif.metadata`, не умеет
заголовок `Require: onvif-replay` и берёт логин и пароль только в адресе — тогда они видны в
списке процессов компьютера стенда. Здесь учётные данные идут только в заголовке Authorization.

Только чтение потока: метод каждого запроса проверяет табу (`core.taboo.check_rtsp`, AD-7).
Транспорт — RTP поверх TCP (interleaved, RFC 2326 §10.12): через NAT и VPN стенда UDP
доходит не всегда, а TCP держит один сокет. UDP — только по просьбе (`setup(udp=True)`): лишь он
показывает потери в самой сети, TCP их прячет повторной отправкой.

Клиент не потокобезопасен: один экземпляр — одна сессия в одном потоке.
"""

import base64
import contextlib
import hashlib
import os
import re
import select
import socket
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import unquote, urlsplit, urlunsplit

from qa_tool.core.taboo import check_rtsp

# Пределы ответа камеры — недоверенных данных (security.md): строка заголовка, число строк и
# тело (SDP, текст ошибки) у настоящих камер на порядки меньше; больше — сбой или атака
MAX_LINE = 8192
MAX_HEADERS = 100
MAX_BODY = 1 << 20
RECV_SIZE = 1 << 16
POLL_S = 0.5  # как часто `watch` проверяет «Стоп», когда камера молчит
HEAD_END = re.compile(rb"\r?\n\r?\n")
UDP_BUFFER = 4 << 20  # ключевой кадр приходит пачкой: буфер по умолчанию дал бы ложные потери
UDP_BATCH = 256  # пакетов за один заход: ответы RTSP и «Стоп» не должны ждать конца всплеска
# пустой заголовок RTP: брандмауэр компьютера стенда пропускает ответ на исходящий пакет
UDP_OPENING = b"\x80\x00" + bytes(10)
UDP_PORT_TRIES = 32
UDP_LAST_PORT = 65534
UDP_MAX_PORT = 65535
SESSION_TIMEOUT_S = 60.0  # RFC 2326 §12.37: столько живёт сессия, если камера не сказала иначе
TEARDOWN_S = 2.0  # сессия всё равно умрёт по сроку, а «Стоп» обещан за секунды
RTSP_UNAUTHORIZED = 401
PASSWORD_REFUSED_CODES = (RTSP_UNAUTHORIZED, 403)
JPEG_PAYLOAD_TYPE = "26"  # RFC 3551: статический тип, `a=rtpmap` для него необязателен


class RtspError(ConnectionError):
    """Ответ RTSP не 2xx (код — в `status`, запрос — в `method`) или соединение оборвалось
    (`status` = 0)."""

    def __init__(self, message: str, status: int = 0, method: str = ""):
        super().__init__(message)
        self.status = status
        self.method = method


@dataclass
class Response:
    """Ответ RTSP: код, причина, заголовки (имена в нижнем регистре), тело."""

    status: int
    reason: str
    headers: dict[str, str]
    body: str = ""


@dataclass
class Track:
    """Дорожка из SDP: тип (video, audio, application), кодек, адрес для SETUP и частота
    меток времени RTP, Гц (из rtpmap; для видео по RFC 3551 почти всегда 90 000); `fmtp` —
    параметры формата из SDP без его номера: в них наборы параметров кодека; `payload_type` —
    первый формат строки `m=`, его камера и шлёт."""

    media: str
    encoding: str
    control: str
    clock_rate: int = 90_000
    fmtp: str = ""
    payload_type: str = ""


@dataclass(frozen=True)
class RtpPacket:
    """Пакет RTP из канала interleaved или из UDP (тогда канал 0).

    `payload` — без заголовка; `timestamp` — метка времени RTP; `extension` — расширение
    заголовка целиком (профиль, длина, данные) или пусто: в архиве ONVIF в нём время кадра
    и признак ключевого кадра. `sequence` — номер RTP, −1 у битого пакета; `arrived` — момент
    приёма по `time.perf_counter()`: шаг `monotonic` на Windows около 15 мс, а джиттер меряется
    десятками миллисекунд.
    """

    channel: int
    marker: bool
    payload: bytes
    timestamp: int = 0
    extension: bytes = b""
    sequence: int = 0
    arrived: float = 0.0


class WatchEnd(Enum):
    """Чем закончилось чтение потока (`RtspClient.watch`)."""

    TIME_UP = "срок вышел"
    STOPPED = "остановлено"
    SILENT = "камера молчит"
    BROKEN = "соединение оборвалось"
    REFUSED = "камера отвергла пароль"


@dataclass
class RtspClient:
    """Сессия RTSP с одной камерой: connect → describe → setup → play → watch или read_packets
    → close.

    Args:
        url: rtsp://адрес[:порт]/путь; логин и пароль из адреса переносятся в заголовок.
        user, password: учётные данные камеры.
        timeout: тайм-аут сети на одну операцию, с.
        extra_headers: добавляются к каждому запросу (например, `Require: onvif-replay`).

    `exchanges` — строки «метод адрес → код» для доказательств протокола; учётных данных в них
    нет: адрес очищен от логина, заголовок Authorization не записывается. `rtp_channel` — канал
    видео, который назначила камера; `udp_buffer` — фактический буфер приёма UDP, байт;
    `rejected` — камера отвергла пароль: больше с ним ничего не шлём, даже TEARDOWN;
    `ping_late_s` — насколько позже послать первое удержание, с: потоки одной камеры не должны
    слать его разом, иначе при отказе пароль уйдёт от каждого; `basic_used` — камера запросила
    вход Basic, пароль ушёл почти открытым.
    """

    url: str
    user: str = ""
    password: str = ""
    timeout: float = 10.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    _sock: socket.socket | None = field(default=None, repr=False)
    _buf: bytearray = field(default_factory=bytearray, repr=False)
    _arrived: float = 0.0
    _cseq: int = 0
    _session: str = ""
    _challenge: dict[str, str] = field(default_factory=dict, repr=False)
    _nc: int = 0
    _lease_s: float = SESSION_TIMEOUT_S
    _ping_method: str = "GET_PARAMETER"  # часть камер его не умеет — тогда OPTIONS
    _ping_logged: bool = False
    _ping_failed: bool = False
    _answer_s: float = 0.0  # 0 — ответ ждём `timeout`
    _udp: socket.socket | None = field(default=None, repr=False)
    _udp_rtcp: socket.socket | None = field(default=None, repr=False)
    _camera_ip: str = ""
    exchanges: list[str] = field(default_factory=list)
    rtp_channel: int = 0
    udp_buffer: int = 0
    rejected: bool = False
    ping_late_s: float = 0.0
    basic_used: bool = False

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.username:  # логин из адреса — в заголовок: адрес уходит в журналы и SDP
            self.user = self.user or unquote(parts.username)
            self.password = self.password or unquote(parts.password or "")
            host = parts.hostname + (f":{parts.port}" if parts.port else "")
            self.url = urlunsplit(parts._replace(netloc=host))

    # --- соединение ----------------------------------------------------------------------------

    def connect(self) -> None:
        """Открыть TCP-соединение с камерой (порт из адреса, по умолчанию 554).

        Raises:
            OSError: камера не отвечает на порту RTSP.
        """
        parts = urlsplit(self.url)
        self._sock = socket.create_connection(
            (parts.hostname, parts.port or 554), timeout=self.timeout
        )
        self._buf.clear()

    def close(self) -> None:
        """Завершить сессию (TEARDOWN, если она открыта) и закрыть соединение."""
        if self._sock is None:
            return
        if self._session and not self.rejected:
            # камера уже закрыла сессию — освобождать нечего
            with contextlib.suppress(OSError, RtspError, ValueError):
                self._answer_s = TEARDOWN_S
                self._sock.settimeout(TEARDOWN_S)
                self.request("TEARDOWN", self.url)
        self._sock.close()
        self._sock = None
        for udp in (self._udp, self._udp_rtcp):
            if udp is not None:
                udp.close()
        self._udp = self._udp_rtcp = None

    def __enter__(self) -> "RtspClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- запросы -------------------------------------------------------------------------------

    def _authorization(self, method: str, uri: str) -> str:
        if not self._challenge or not self.user:
            return ""
        if self._challenge["scheme"] == "basic":
            self.basic_used = True
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return f"Basic {token}"
        realm, nonce = self._challenge.get("realm", ""), self._challenge.get("nonce", "")
        # MD5 задан RFC 2617 для Digest; камеры другого алгоритма в RTSP не предлагают
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()  # noqa: S324
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # noqa: S324
        header = f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
        if "auth" in self._challenge.get("qop", "").split(","):
            self._nc += 1
            nc, cnonce = f"{self._nc:08x}", os.urandom(8).hex()
            digest = f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}"
            header += f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        else:
            digest = f"{ha1}:{nonce}:{ha2}"
        response = hashlib.md5(digest.encode()).hexdigest()  # noqa: S324
        if "opaque" in self._challenge:
            header += f', opaque="{self._challenge["opaque"]}"'
        return f'{header}, response="{response}"'

    def _compose(self, method: str, uri: str, headers: dict[str, str]) -> bytes:
        self._cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self._cseq}", "User-Agent: QA-tool"]
        lines += [f"{k}: {v}" for k, v in {**self.extra_headers, **headers}.items()]
        if self._session:
            lines.append(f"Session: {self._session}")
        auth = self._authorization(method, uri)
        if auth:
            lines.append(f"Authorization: {auth}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _send(self, method: str, uri: str, headers: dict[str, str]) -> Response:
        self._sock.sendall(self._compose(method, uri, headers))
        return self._read_response()

    def _ping(self) -> None:
        """Продлить сессию, не дожидаясь ответа.

        `request` здесь нельзя: он ждёт ответ и выбрасывает весь RTP, что придёт за это время, а
        разрыв номеров считается потерей у камеры. Ответ разбирает цикл `watch`.
        """
        check_rtsp(self._ping_method)
        self._sock.sendall(self._compose(self._ping_method, self.url, {}))

    def _ping_answered(self, response: Response) -> bool:
        """Разобрать ответ на удержание; True — повторить сразу.

        Ждать следующей отправки при отказе нельзя: она придётся на конец срока сессии.
        """
        ok = 200 <= response.status < 300
        if response.status == RTSP_UNAUTHORIZED:
            self._challenge = _parse_challenge(response.headers.get("www-authenticate", ""))
        elif not ok:
            self._ping_method = "OPTIONS"
        if response.status in PASSWORD_REFUSED_CODES:
            # первый отказ — обычно устаревший nonce или запрет метода; второй подряд значит, что
            # камера больше не принимает пароль, а новый запрос с ним может приблизить блокировку
            self.rejected = self._ping_failed
        if not ok or not self._ping_logged:
            self.exchanges.append(f"удержание сессии → {response.status} {response.reason}".strip())
            self._ping_logged = True
        retry = not ok and not self._ping_failed
        self._ping_failed = not ok
        return retry

    def _fill(self) -> None:
        """Дочитать из соединения в буфер; тайм-аут ничего не портит: начатый кадр остаётся в нём.

        Raises:
            RtspError: камера закрыла соединение.
            TimeoutError: за тайм-аут сокета ничего не пришло.
        """
        data = self._sock.recv(RECV_SIZE)
        if not data:
            middle = "посреди пакета" if self._buf else ""
            raise RtspError(f"камера закрыла соединение RTSP {middle}".strip())
        self._arrived = time.perf_counter()
        self._buf += data

    def _take_frame(self) -> "RtpPacket | Response | None":
        """Снять из буфера кадр целиком: пакет RTP (`$`, RFC 2326 §10.12) или ответ RTSP.

        Returns:
            Кадр или None, если он ещё не пришёл целиком.

        Raises:
            RtspError: пришёл не RTSP или ответ за пределами допустимого.
        """
        buf = self._buf
        skip = 0
        while skip < len(buf) and buf[skip] in b"\r\n":
            skip += 1
        del buf[:skip]
        if not buf:
            return None
        if buf[0] != 0x24:
            return self._take_response()
        if len(buf) < 4:
            return None
        end = 4 + struct.unpack_from("!H", buf, 2)[0]
        if len(buf) < end:
            return None
        packet = _rtp_packet(buf[1], bytes(buf[4:end]), self._arrived)
        del buf[:end]
        return packet

    def _take_response(self) -> "Response | None":
        buf = self._buf
        line_end = buf.find(b"\n")
        if line_end < 0 and len(buf) <= MAX_LINE:
            return None
        line = bytes(buf[: line_end if line_end >= 0 else MAX_LINE]).decode("latin-1").strip()
        status_line = line.split(" ", 2)
        if len(status_line) < 2 or not status_line[1].isdigit():
            raise RtspError(f"не ответ RTSP: {line[:60]!r}")
        head_end = HEAD_END.search(buf)
        if head_end is None:
            if len(buf) > MAX_LINE * MAX_HEADERS:
                raise RtspError("заголовки ответа RTSP не заканчиваются")
            return None
        headers = {}
        for raw in bytes(buf[: head_end.start()]).decode("latin-1").splitlines()[1:]:
            if len(headers) >= MAX_HEADERS:
                raise RtspError(f"в ответе RTSP больше {MAX_HEADERS} заголовков")
            key, _, value = raw.partition(":")
            name = key.strip().lower()
            # камера может предложить оба способа входа: Basic не должен заменить Digest
            if name != "www-authenticate" or not _offers_digest(headers.get(name, "")):
                headers[name] = value.strip()
        raw_length = headers.get("content-length", "0") or "0"
        if not raw_length.isdigit() or int(raw_length) > MAX_BODY:
            raise RtspError(f"недопустимый Content-Length в ответе RTSP: {raw_length[:20]!r}")
        total = head_end.end() + int(raw_length)
        if len(buf) < total:
            return None
        body = bytes(buf[head_end.end() : total]).decode("utf-8", "replace")
        del buf[:total]
        reason = status_line[2] if len(status_line) > 2 else ""
        return Response(int(status_line[1]), reason, headers, body)

    def _read_frame(self) -> "RtpPacket | Response":
        """Следующий кадр из соединения.

        Raises:
            RtspError: соединение закрыто или пришёл не RTSP.
            TimeoutError: кадр не пришёл за тайм-аут сокета.
        """
        while (frame := self._take_frame()) is None:
            self._fill()
        return frame

    def _read_response(self) -> Response:
        # после PLAY камера шлёт RTP сразу, и пакеты могут прийти раньше ответа — пропускаем.
        # Пакеты сбрасывают тайм-аут сокета, поэтому без общего срока камера, которая шлёт видео
        # и не отвечает на запрос, держала бы нас вечно
        deadline = time.monotonic() + (self._answer_s or self.timeout)
        while not isinstance(frame := self._take_frame(), Response):
            if time.monotonic() >= deadline:
                raise TimeoutError("камера шлёт видео, но не отвечает на запрос")
            if frame is None:
                self._fill()
        return frame

    def request(self, method: str, uri: str, headers: dict[str, str] | None = None) -> Response:
        """Отправить запрос; на 401 — один повтор с Digest или Basic.

        Raises:
            TabooViolation: метод меняет камеру (табу, AD-7) — запрос не уходит.
            RtspError: ответ не 2xx или обрыв соединения.
        """
        check_rtsp(method)
        response = self._send(method, uri, headers or {})
        if response.status == RTSP_UNAUTHORIZED and self.user:
            self._challenge = _parse_challenge(response.headers.get("www-authenticate", ""))
            response = self._send(method, uri, headers or {})
        self.exchanges.append(f"{method} {uri} → {response.status} {response.reason}".strip())
        refused = response.status == RTSP_UNAUTHORIZED or (
            response.status in PASSWORD_REFUSED_CODES and method == "DESCRIBE"
        )
        self.rejected = self.rejected or refused
        if not 200 <= response.status < 300:
            raise RtspError(
                f"{method} → {response.status} {response.reason}".strip(), response.status, method
            )
        return response

    # --- сценарий чтения -----------------------------------------------------------------------

    def describe(self) -> tuple[str, list[Track]]:
        """SDP потока и его дорожки.

        Raises:
            RtspError: камера не отдала описание потока.
        """
        response = self.request("DESCRIBE", self.url, {"Accept": "application/sdp"})
        base = response.headers.get("content-base", self.url)
        return response.body, parse_sdp(response.body, base)

    def _open_udp(self) -> tuple[int, int]:
        """Пара портов приёма: чётный RTP и следующий RTCP (RFC 3550 §11)."""
        local = self._sock.getsockname()[0]
        for _ in range(UDP_PORT_TRIES):
            rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtp.bind((local, 0))
            port = rtp.getsockname()[1]
            rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if port % 2 == 0 and port < UDP_LAST_PORT:
                    rtcp.bind((local, port + 1))
                    rtp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_BUFFER)
                    # система молча режет запрос по своему пределу (на Linux — rmem_max)
                    self.udp_buffer = rtp.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                    if sys.platform == "linux":  # ядро отвечает удвоенным размером (socket(7))
                        self.udp_buffer //= 2
                    rtp.setblocking(False)
                    self._udp, self._udp_rtcp = rtp, rtcp
                    return port, port + 1
            except OSError:
                pass  # следующий порт занят — пробуем другую пару
            rtp.close()
            rtcp.close()
        raise OSError("не нашлось свободной пары портов UDP")

    def setup(self, track: Track, channel: int = 0, udp: bool = False) -> None:
        """Подключить дорожку.

        Args:
            track: дорожка из SDP.
            channel: по TCP — канал соединения для RTP (RTCP — в следующем).
            udp: принимать по UDP; пакеты приходят с каналом 0.

        Raises:
            RtspError: камера отказала (часто 461 — не умеет такой транспорт).
            OSError: нет свободных портов UDP.
        """
        if udp:
            first, second = self._open_udp()
            transport = f"RTP/AVP;unicast;client_port={first}-{second}"
        else:
            transport = f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"
        response = self.request("SETUP", track.control, {"Transport": transport})
        if udp:
            self._camera_ip = self._sock.getpeername()[0]
            camera_port = re.search(r"server_port=(\d+)", response.headers.get("transport", ""))
            if camera_port and 0 < int(camera_port[1]) <= UDP_MAX_PORT:
                self._udp.sendto(UDP_OPENING, (self._camera_ip, int(camera_port[1])))
        else:
            assigned = re.search(r"interleaved=(\d+)", response.headers.get("transport", ""))
            self.rtp_channel = int(assigned[1]) if assigned else channel
        session, _, options = response.headers.get("session", "").partition(";")
        self._session = self._session or session.strip()
        lease = re.search(r"timeout\s*=\s*(\d+)", options, re.IGNORECASE)
        if lease and int(lease[1]):
            self._lease_s = float(lease[1])

    def play(self, headers: dict[str, str] | None = None) -> None:
        """Запустить поток с начала (`Range: npt=0-`); `headers` — например, Range архива.

        Raises:
            RtspError: камера не запустила поток.
        """
        self.request("PLAY", self.url, {"Range": "npt=0-", **(headers or {})})

    def read_packets(
        self,
        seconds: float,
        limit: int = 10_000,
        until: Callable[[RtpPacket], bool] | None = None,
    ) -> list[RtpPacket]:
        """Пакеты RTP за `seconds` секунд или до `limit` штук; RTCP-каналы тоже попадают.

        Args:
            seconds: наибольшее время чтения, с.
            limit: наибольшее число пакетов.
            until: вызывается на каждый пакет; True — пакет последний, чтение закончено.

        Остановка по тишине: если камера ничего не шлёт дольше тайм-аута соединения,
        чтение заканчивается раньше срока.
        """
        deadline = time.monotonic() + seconds
        packets: list[RtpPacket] = []
        self._sock.settimeout(max(0.5, min(self.timeout, seconds)))
        try:
            while time.monotonic() < deadline and len(packets) < limit:
                frame = self._read_frame()
                if isinstance(frame, RtpPacket):  # ответы на keep-alive не нужны
                    packets.append(frame)
                    if until is not None and until(frame):
                        break
        except (TimeoutError, RtspError):
            pass  # тишина дольше тайм-аута или обрыв — вернуть то, что успело прийти
        return packets

    def _accept_udp(self, data: bytes, sender: tuple) -> RtpPacket | None:
        """Пакет UDP, если он с адреса камеры: порт приёма открыт для всей сети стенда."""
        if sender[0] != self._camera_ip:
            return None
        return _rtp_packet(0, data, time.perf_counter())

    def _drain_udp(self, on_packet: Callable[[RtpPacket], None]) -> bool:
        """Отдать всё, что пришло по UDP; True — хоть что-то пришло от камеры."""
        heard = False
        for _ in range(UDP_BATCH):
            try:
                data, sender = self._udp.recvfrom(RECV_SIZE)
            except (BlockingIOError, ConnectionResetError):
                # ConnectionResetError — Windows так сообщает об ICMP «порт закрыт» на наш пакет
                break
            packet = self._accept_udp(data, sender)
            if packet is not None:
                heard = True
                on_packet(packet)
        return heard

    def _fill_when_ready(self) -> None:
        """Дождаться данных; при UDP ждём оба сокета, иначе пакеты лежали бы до конца тайм-аута."""
        if self._udp is None:
            self._fill()
            return
        ready, _, _ = select.select([self._sock, self._udp], [], [], POLL_S)
        if self._sock in ready:
            self._fill()

    def watch(
        self,
        on_packet: Callable[[RtpPacket], None],
        seconds: float,
        stop: Callable[[], bool] = lambda: False,
        silence_s: float | None = None,
    ) -> WatchEnd:
        """Читать поток, отдавая пакеты `on_packet`; ничего не копит, поэтому годится на часы.

        Args:
            on_packet: вызывается на каждый пакет RTP.
            seconds: наибольшее время чтения, с.
            stop: опрашивается постоянно и на молчащей камере; True — закончить.
            silence_s: столько секунд без пакетов считается тишиной; по умолчанию — тайм-аут.

        Returns:
            Причина окончания. После `SILENT` соединение исправно, после `BROKEN` — нет.

        Раз в половину срока сессии шлёт запрос удержания: без него камера закрыла бы сессию.
        Камера, которая на него не отвечает, чтение не останавливает. Тишина считается только по
        видео: RTCP идёт, пока жива сессия, даже если кодировщик умер.
        """
        silence_s = self.timeout if silence_s is None else silence_s
        deadline = time.perf_counter() + seconds
        heard = time.perf_counter()
        next_ping = heard + self._lease_s / 2 + min(self.ping_late_s, self._lease_s / 4)
        self._sock.settimeout(POLL_S)
        while True:
            now = time.perf_counter()
            if stop():
                return WatchEnd.STOPPED
            if now >= deadline:
                return WatchEnd.TIME_UP
            if now - heard >= silence_s:
                return WatchEnd.SILENT
            try:
                if now >= next_ping:
                    next_ping = now + self._lease_s / 2
                    self._ping()
                if self._udp is not None and self._drain_udp(on_packet):
                    heard = time.perf_counter()
                frame = self._take_frame()
                if frame is None:
                    self._fill_when_ready()
                    continue
            except TimeoutError:
                continue
            except (RtspError, OSError):
                return WatchEnd.BROKEN
            if isinstance(frame, RtpPacket):
                if frame.channel == self.rtp_channel:
                    heard = time.perf_counter()
                on_packet(frame)
            elif self._ping_answered(frame):
                next_ping = now
            if self.rejected:
                return WatchEnd.REFUSED


def _offers_digest(header: str) -> bool:
    return header.lower().startswith("digest")


def _parse_challenge(header: str) -> dict[str, str]:
    scheme = "basic" if header.lower().startswith("basic") else "digest"
    return {"scheme": scheme, **dict(re.findall(r'(\w+)="?([^",]*)"?', header))}


def _rtp_packet(channel: int, packet: bytes, arrived: float = 0.0) -> RtpPacket:
    """Разбор пакета RTP (RFC 3550 §5.1): маркер, метка времени, расширение, полезная нагрузка."""
    if len(packet) < 12:
        return RtpPacket(channel, False, b"", sequence=-1, arrived=arrived)
    marker = bool(packet[1] & 0x80)
    # RFC 3550 §5.1: иначе байты выравнивания попали бы в кадр и в битрейт
    padding = packet[-1] if packet[0] & 0x20 else 0
    sequence, timestamp = struct.unpack("!HI", packet[2:8])
    offset = 12 + 4 * (packet[0] & 0x0F)
    extension = b""
    # расширение заголовка: в архиве ONVIF (Profile G) в нём время кадра и признак ключевого
    if packet[0] & 0x10 and len(packet) >= offset + 4:
        end = offset + 4 + 4 * struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
        extension, offset = packet[offset:end], end
    payload = packet[offset : len(packet) - padding]
    return RtpPacket(channel, marker, payload, timestamp, extension, sequence, arrived)


def _format_of(line: str) -> str:
    """Номер формата из строки `a=rtpmap:96 ...` или `a=fmtp:96 ...`."""
    return line.partition(":")[2].split(" ")[0]


def _takes_format(track: Track, line: str) -> bool:
    """Строка `a=rtpmap` или `a=fmtp` относится к формату дорожки.

    Форматов в `m=` бывает несколько, а камера шлёт первый. Камера, у которой номера в `m=` и
    `a=` не сходятся, всё равно читается: такой формат берётся, пока подходящего не нашлось.
    """
    return _format_of(line) == track.payload_type or not track.encoding


def parse_sdp(sdp: str, base: str) -> list[Track]:
    """Дорожки SDP: тип, кодек (rtpmap) и полный адрес управления для SETUP (RFC 4566)."""
    tracks: list[Track] = []
    for raw in sdp.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            fields = line[2:].split()
            if not fields:
                tracks.append(Track("", "", base))
                continue
            number = fields[3] if len(fields) > 3 else ""
            encoding = "JPEG" if number == JPEG_PAYLOAD_TYPE else ""
            tracks.append(Track(fields[0], encoding, base, payload_type=number))
        elif tracks and line.startswith("a=rtpmap:") and _takes_format(tracks[-1], line):
            encoding, _, rest = line.split(" ", 1)[-1].partition("/")
            tracks[-1].encoding = encoding
            rate = rest.split("/")[0]
            if rate.isdigit() and int(rate):
                tracks[-1].clock_rate = int(rate)
        elif tracks and line.startswith("a=fmtp:"):
            if _format_of(line) == tracks[-1].payload_type or not tracks[-1].fmtp:
                tracks[-1].fmtp = line.partition(" ")[2].strip()
        elif tracks and line.startswith("a=control:"):
            control = line[len("a=control:") :]
            if "://" in control:
                tracks[-1].control = control
            elif control != "*":
                tracks[-1].control = base.rstrip("/") + "/" + control
    return tracks
