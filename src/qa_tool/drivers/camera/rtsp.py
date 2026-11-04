"""Минимальный RTSP-клиент для чтения потоков камеры: метаданные ONVIF (Profile M) и архив
Profile G.

Зачем свой клиент, а не ffmpeg: ffmpeg не разбирает дорожку `vnd.onvif.metadata`, не умеет
заголовок `Require: onvif-replay` и берёт логин и пароль только в адресе — тогда они видны в
списке процессов компьютера стенда. Здесь учётные данные идут только в заголовке Authorization.

Только чтение потока: метод каждого запроса проверяет табу (`core.taboo.check_rtsp`, AD-7).
Транспорт — RTP поверх TCP (interleaved, RFC 2326 §10.12): через NAT и VPN стенда UDP
доходит не всегда, а TCP держит один сокет.

Клиент не потокобезопасен: один экземпляр — одна сессия в одном потоке.
"""

import base64
import contextlib
import hashlib
import io
import os
import re
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit, urlunsplit

from qa_tool.core.taboo import check_rtsp


class RtspError(ConnectionError):
    """Ответ RTSP не 2xx (код — в `status`) или соединение оборвалось (`status` = 0)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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
    меток времени RTP, Гц (из rtpmap; для видео по RFC 3551 почти всегда 90 000)."""

    media: str
    encoding: str
    control: str
    clock_rate: int = 90_000


@dataclass(frozen=True)
class RtpPacket:
    """Пакет RTP из канала interleaved.

    `payload` — без заголовка; `timestamp` — метка времени RTP; `extension` — расширение
    заголовка целиком (профиль, длина, данные) или пусто: в архиве ONVIF в нём время кадра
    и признак ключевого кадра.
    """

    channel: int
    marker: bool
    payload: bytes
    timestamp: int = 0
    extension: bytes = b""


@dataclass
class RtspClient:
    """Сессия RTSP с одной камерой: connect → describe → setup → play → read_packets → close.

    Args:
        url: rtsp://адрес[:порт]/путь; логин и пароль из адреса переносятся в заголовок.
        user, password: учётные данные камеры.
        timeout: тайм-аут сети на одну операцию, с.
        extra_headers: добавляются к каждому запросу (например, `Require: onvif-replay`).

    `exchanges` — строки «метод адрес → код» для доказательств протокола; учётных данных в них
    нет: адрес очищен от логина, заголовок Authorization не записывается.
    """

    url: str
    user: str = ""
    password: str = ""
    timeout: float = 10.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    _sock: socket.socket | None = field(default=None, repr=False)
    _file: io.BufferedRWPair | None = field(default=None, repr=False)
    _cseq: int = 0
    _session: str = ""
    _challenge: dict[str, str] = field(default_factory=dict, repr=False)
    _nc: int = 0
    exchanges: list[str] = field(default_factory=list)

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
        self._file = self._sock.makefile("rwb")

    def close(self) -> None:
        """Завершить сессию (TEARDOWN, если она открыта) и закрыть соединение."""
        if self._sock is None:
            return
        if self._session:
            # камера уже закрыла сессию или сокет испорчен тайм-аутом — освобождать нечего
            with contextlib.suppress(OSError, RtspError, ValueError):
                self.request("TEARDOWN", self.url)
        self._sock.close()
        self._sock = self._file = None

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

    def _send(self, method: str, uri: str, headers: dict[str, str]) -> Response:
        self._cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self._cseq}", "User-Agent: QA-tool"]
        lines += [f"{k}: {v}" for k, v in {**self.extra_headers, **headers}.items()]
        if self._session:
            lines.append(f"Session: {self._session}")
        auth = self._authorization(method, uri)
        if auth:
            lines.append(f"Authorization: {auth}")
        self._file.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        self._file.flush()
        return self._read_response()

    def _read_frame(self) -> "RtpPacket | Response":
        """Следующее из соединения: пакет RTP (кадр `$`, RFC 2326 §10.12) или ответ RTSP.

        Raises:
            RtspError: соединение закрыто или пришёл не RTSP.
        """
        while True:
            first = self._file.read(1)
            if not first:
                raise RtspError("камера закрыла соединение RTSP")
            if first == b"$":
                head = self._file.read(3)
                data = self._file.read(struct.unpack("!H", head[1:3])[0])
                return _rtp_packet(head[0], data)
            if first not in b"\r\n":
                break
        line = (first + self._file.readline()).decode("latin-1").strip()
        status_line = line.split(" ", 2)
        if len(status_line) < 2 or not status_line[1].isdigit():
            raise RtspError(f"не ответ RTSP: {line[:60]!r}")
        headers = {}
        while raw := self._file.readline().decode("latin-1").strip():
            key, _, value = raw.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        body = self._file.read(length).decode("utf-8", "replace") if length else ""
        reason = status_line[2] if len(status_line) > 2 else ""
        return Response(int(status_line[1]), reason, headers, body)

    def _read_response(self) -> Response:
        # после PLAY камера шлёт RTP сразу, и пакеты могут прийти раньше ответа — пропускаем
        while not isinstance(frame := self._read_frame(), Response):
            pass
        return frame

    def request(self, method: str, uri: str, headers: dict[str, str] | None = None) -> Response:
        """Отправить запрос; на 401 — один повтор с Digest или Basic.

        Raises:
            TabooViolation: метод меняет камеру (табу, AD-7) — запрос не уходит.
            RtspError: ответ не 2xx или обрыв соединения.
        """
        check_rtsp(method)
        response = self._send(method, uri, headers or {})
        if response.status == 401 and self.user:
            self._challenge = _parse_challenge(response.headers.get("www-authenticate", ""))
            response = self._send(method, uri, headers or {})
        self.exchanges.append(f"{method} {uri} → {response.status} {response.reason}".strip())
        if not 200 <= response.status < 300:
            raise RtspError(
                f"{method} → {response.status} {response.reason}".strip(), response.status
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

    def setup(self, track: Track, channel: int = 0) -> None:
        """Подключить дорожку: её RTP пойдёт в канал `channel` соединения (RTCP — в следующий).

        Raises:
            RtspError: камера отказала (часто 461 — не умеет TCP).
        """
        transport = f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"
        response = self.request("SETUP", track.control, {"Transport": transport})
        self._session = self._session or response.headers.get("session", "").split(";")[0]

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
        чтение заканчивается раньше срока — после этого сессию можно только закрыть.
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


def _parse_challenge(header: str) -> dict[str, str]:
    scheme = "basic" if header.lower().startswith("basic") else "digest"
    return {"scheme": scheme, **dict(re.findall(r'(\w+)="?([^",]*)"?', header))}


def _rtp_packet(channel: int, packet: bytes) -> RtpPacket:
    """Разбор пакета RTP (RFC 3550 §5.1): маркер, метка времени, расширение, полезная нагрузка."""
    if len(packet) < 12:
        return RtpPacket(channel, False, b"")
    marker = bool(packet[1] & 0x80)
    timestamp = struct.unpack("!I", packet[4:8])[0]
    offset = 12 + 4 * (packet[0] & 0x0F)
    extension = b""
    # расширение заголовка: в архиве ONVIF (Profile G) в нём время кадра и признак ключевого
    if packet[0] & 0x10 and len(packet) >= offset + 4:
        end = offset + 4 + 4 * struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
        extension, offset = packet[offset:end], end
    return RtpPacket(channel, marker, packet[offset:], timestamp, extension)


def parse_sdp(sdp: str, base: str) -> list[Track]:
    """Дорожки SDP: тип, кодек (rtpmap) и полный адрес управления для SETUP (RFC 4566)."""
    tracks: list[Track] = []
    for raw in sdp.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            tracks.append(Track(line[2:].split()[0], "", base))
        elif tracks and line.startswith("a=rtpmap:"):
            encoding, _, rest = line.split(" ", 1)[-1].partition("/")
            tracks[-1].encoding = encoding
            rate = rest.split("/")[0]
            if rate.isdigit() and int(rate):
                tracks[-1].clock_rate = int(rate)
        elif tracks and line.startswith("a=control:"):
            control = line[len("a=control:") :]
            if "://" in control:
                tracks[-1].control = control
            elif control != "*":
                tracks[-1].control = base.rstrip("/") + "/" + control
    return tracks
