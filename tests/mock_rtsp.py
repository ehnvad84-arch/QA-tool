"""Минимальный RTSP-сервер виртуальной камеры: живой поток, «архив» и метаданные ONVIF.

Видео — H.264, сгенерированный один раз встроенным ffmpeg (OpenH264) и закешированный;
отдаётся по RTP поверх TCP (interleaved, RFC 2326 §10.12), упаковка — RFC 6184 (одиночные
NAL и FU-A). Пути: `/live` — видео по кругу и, если `metadata`, вторая дорожка с XML
MetadataStream раз в 0,5 с (ONVIF Streaming, vnd.onvif.metadata — так отдают метаданные камеры
Profile M); `/metadata` — только метаданные; `/replay/<запись>` — 12 с и тишина, как конец
архива, с расширением RTP архива ONVIF (время кадра, признак ключевого кадра). Канал interleaved
каждой дорожки — тот, что попросил клиент в SETUP. `require_unsupported` — камера без поддержки
`Require: onvif-replay`: на такой запрос отвечает 551, как велит RFC 2326.
UDP не поддерживается: клиент получает 461 и должен просить TCP — так ведут себя многие камеры.
"""

import base64
import datetime
import hashlib
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import threading
import time
from pathlib import Path

FPS = 10
SECONDS = 12
WIDTH, HEIGHT = 320, 240
CACHE = Path(os.environ.get("QA_TOOL_TEST_CACHE", Path.home() / ".cache" / "qa-tool-test"))
MTU_PAYLOAD = 1400
NTP_EPOCH = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)


def onvif_extension(moment: datetime.datetime, clean: bool) -> bytes:
    """Расширение RTP архива ONVIF: профиль 0xABAC, 3 слова — NTP-время, флаги C/E/D, CSeq."""
    ntp = (moment - NTP_EPOCH).total_seconds()
    seconds, fraction = int(ntp), int((ntp % 1) * 2**32)
    flags = 0x80 if clean else 0
    return b"\xab\xac\x00\x03" + struct.pack("!IIBBH", seconds, fraction, flags, 0, 0)


def find_ffmpeg() -> str | None:
    """ffmpeg из PATH или из кэша сборки (packaging/fetch.py)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in sorted(
        (Path.home() / ".cache" / "qa-tool-build").glob("ffmpeg-*/*/bin/ffmpeg*")
    ):
        if candidate.name in ("ffmpeg", "ffmpeg.exe"):
            return str(candidate)
    return None


def test_video() -> bytes | None:
    """Annex-B H.264 с разделителями кадров (AUD); None — ffmpeg не найден."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    key = hashlib.sha1(f"{WIDTH}x{HEIGHT}@{FPS}x{SECONDS}".encode()).hexdigest()[:10]
    path = CACHE / f"mock-{key}.h264"
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        subprocess.run(
            [
                ffmpeg,
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={SECONDS}",
                "-c:v",
                "libopenh264",
                "-g",
                str(FPS),
                "-bsf:v",
                "h264_metadata=aud=insert",
                "-f",
                "h264",
                str(tmp),
            ],
            check=True,
            timeout=120,
        )
        tmp.replace(path)
    return path.read_bytes()


def split_nals(stream: bytes) -> list[bytes]:
    parts = stream.replace(b"\x00\x00\x00\x01", b"\x00\x00\x01").split(b"\x00\x00\x01")
    return [p for p in parts if p]


def access_units(stream: bytes) -> tuple[list[list[bytes]], bytes, bytes]:
    """Кадры (списки NAL без AUD), SPS и PPS."""
    frames, current, sps, pps = [], [], b"", b""
    for nal in split_nals(stream):
        kind = nal[0] & 0x1F
        if kind == 9:  # AUD — начало нового кадра
            if current:
                frames.append(current)
            current = []
            continue
        if kind == 7:
            sps = sps or nal
        elif kind == 8:
            pps = pps or nal
        current.append(nal)
    if current:
        frames.append(current)
    return frames, sps, pps


def rtp_packets(nal: bytes, last_in_frame: bool) -> list[tuple[bytes, bool]]:
    """Полезная нагрузка RTP для одного NAL: целиком или кусками FU-A (RFC 6184 §5.8)."""
    if len(nal) <= MTU_PAYLOAD:
        return [(nal, last_in_frame)]
    header, body = nal[0], nal[1:]
    indicator = (header & 0xE0) | 28
    chunks = [body[i : i + MTU_PAYLOAD - 2] for i in range(0, len(body), MTU_PAYLOAD - 2)]
    out = []
    for i, chunk in enumerate(chunks):
        start, end = i == 0, i == len(chunks) - 1
        fu_header = (0x80 if start else 0) | (0x40 if end else 0) | (header & 0x1F)
        out.append((bytes([indicator, fu_header]) + chunk, last_in_frame and end))
    return out


class _Session:
    def __init__(
        self, path: str, frames, write, channels: dict[str, int], timeout_s=0, on_expire=None
    ):
        self.path, self.frames, self.write = path, frames, write
        self.timeout_s, self.on_expire = timeout_s, on_expire
        self.last_seen = time.monotonic()
        self.channels = channels  # дорожка («video», «metadata») → канал interleaved
        self.stop = threading.Event()
        self.seq = {"video": 1, "metadata": 1}

    def _send(
        self, kind: str, payload: bytes, marker: bool, timestamp: int, pt: int, ext: bytes = b""
    ) -> None:
        header = struct.pack(
            "!BBHII",
            0x90 if ext else 0x80,
            (0x80 if marker else 0) | pt,
            self.seq[kind] & 0xFFFF,
            timestamp & 0xFFFFFFFF,
            0x51A0C0DE if kind == "video" else 0x3E7AD47A,
        )
        self.seq[kind] += 1
        packet = header + ext + payload
        channel = self.channels[kind]
        self.write(b"$" + bytes([channel]) + struct.pack("!H", len(packet)) + packet)

    def run(self) -> None:
        if "metadata" in self.channels:
            threading.Thread(target=self._metadata, daemon=True).start()
        if "video" in self.channels:
            self._video()

    def _video(self) -> None:
        if not self.frames:
            self.stop.wait()
            return
        replay = self.path.startswith("/replay")
        index = 0
        started = time.monotonic()
        archive_start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=60)
        while not self.stop.is_set():
            if self.timeout_s and time.monotonic() - self.last_seen > self.timeout_s:
                self.on_expire()
                return
            if index >= len(self.frames):
                if replay:
                    return  # конец архива: дальше камера молчит
                index = 0
            frame = self.frames[index]
            timestamp = (
                int(index * 90000 / FPS) if replay else int((time.monotonic() - started) * 90000)
            )
            ext = b""
            if replay:
                moment = archive_start + datetime.timedelta(seconds=index / FPS)
                ext = onvif_extension(moment, any(nal[0] & 0x1F == 5 for nal in frame))
            for n, nal in enumerate(frame):
                for payload, marker in rtp_packets(nal, n == len(frame) - 1):
                    self._send("video", payload, marker, timestamp, 96, ext)
            index += 1
            self.stop.wait(1 / FPS)

    def _metadata(self) -> None:
        started = time.monotonic()
        while not self.stop.is_set():
            utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?><tt:MetadataStream '
                'xmlns:tt="http://www.onvif.org/ver10/schema"><tt:VideoAnalytics>'
                f'<tt:Frame UtcTime="{utc}"/></tt:VideoAnalytics></tt:MetadataStream>'
            ).encode()
            # документ делится на два пакета: клиент обязан собирать его по биту маркера
            timestamp = int((time.monotonic() - started) * 90000)
            half = len(xml) // 2
            self._send("metadata", xml[:half], False, timestamp, 107)
            self._send("metadata", xml[half:], True, timestamp, 107)
            self.stop.wait(0.5)


class MockRtspServer:
    """RTSP-сервер на 127.0.0.1; без ffmpeg отвечает только на OPTIONS (как раньше)."""

    def __init__(
        self,
        metadata: bool = True,
        require_unsupported: bool = False,
        silent: bool = False,
        session_timeout: int = 0,
        ping: str = "answer",
        ping_delay_s: float = 0.0,
        sets_in_stream: bool = True,
    ):
        self.metadata = metadata
        self.silent = silent  # поток запущен, но камера ничего не шлёт
        self.session_timeout = session_timeout  # с; без запросов сессия закрывается, 0 — нет
        self.ping = ping  # GET_PARAMETER: answer, silent (молчит, но считает), refused (405)
        self.ping_delay_s = ping_delay_s
        self.expired = False
        self.require_unsupported = require_unsupported
        self.headers: list[tuple[str, str, dict[str, str]]] = []  # метод, путь, заголовки
        video = test_video()
        self.frames, sps, pps = access_units(video) if video else ([], b"", b"")
        if not sets_in_stream:  # такие камеры отдают SPS и PPS только в SDP
            self.frames = [[n for n in f if n[0] & 0x1F not in (7, 8)] for f in self.frames]
        self.sprop = ",".join(base64.b64encode(x).decode() for x in (sps, pps) if x)
        self.requests: list[str] = []
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "MockRtspServer":
        threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def tracks(self, path: str) -> list[str]:
        """Дорожки пути по порядку: trackN в SETUP — N-я из них."""
        if path.startswith("/metadata"):
            return ["metadata"]
        return ["video", "metadata"] if path.startswith("/live") and self.metadata else ["video"]

    def sdp(self, path: str) -> str:
        media = ""
        for n, kind in enumerate(self.tracks(path), start=1):
            if kind == "metadata":
                media += "m=application 0 RTP/AVP 107\r\na=rtpmap:107 vnd.onvif.metadata/90000\r\n"
            else:
                media += (
                    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                    f"a=fmtp:96 packetization-mode=1;sprop-parameter-sets={self.sprop}\r\n"
                )
                if path.startswith("/replay"):
                    media += f"a=range:npt=0-{SECONDS}\r\n"
            media += f"a=control:track{n}\r\n"
        return "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=QA-tool mock\r\nt=0 0\r\n" + media

    def _handler(self):
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                lock = threading.Lock()
                session: _Session | None = None
                channels: dict[str, int] = {}

                def write(data: bytes) -> None:
                    with lock:
                        self.wfile.write(data)
                        self.wfile.flush()

                def expire() -> None:
                    server.expired = True
                    self.connection.shutdown(socket.SHUT_RDWR)

                try:
                    while True:
                        line = self.rfile.readline()
                        if not line:
                            break
                        if line.startswith(b"$"):  # RTCP от клиента — пропускаем
                            size = struct.unpack("!H", self.rfile.read(3)[1:])[0]
                            self.rfile.read(size)
                            continue
                        method, url, _ = line.decode("latin-1").split(" ", 2)
                        headers = {}
                        while raw := self.rfile.readline().decode("latin-1").strip():
                            key, _, value = raw.partition(":")
                            headers[key.strip().lower()] = value.strip()
                        if int(headers.get("content-length", 0)):
                            self.rfile.read(int(headers["content-length"]))
                        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
                        path, _, track = path.partition("/track")
                        server.requests.append(f"{method} {path}")
                        server.headers.append((method, path, headers))
                        cseq = headers.get("cseq", "0")
                        if method == "GET_PARAMETER" and server.ping == "refused":
                            write(
                                f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n".encode()
                            )
                            continue
                        if session:
                            session.last_seen = time.monotonic()
                        if method == "GET_PARAMETER" and server.ping == "silent":
                            continue
                        if method == "GET_PARAMETER":
                            time.sleep(server.ping_delay_s)
                        if server.require_unsupported and "require" in headers:
                            write(
                                f"RTSP/1.0 551 Option not supported\r\nCSeq: {cseq}\r\n"
                                f"Unsupported: {headers['require']}\r\n\r\n".encode()
                            )
                            continue
                        extra, body = "", ""
                        if method == "OPTIONS":
                            extra = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n"
                        elif method == "DESCRIBE" and server.frames:
                            body = server.sdp(path)
                            extra = f"Content-Type: application/sdp\r\nContent-Base: {url}/\r\n"
                        elif method == "SETUP":
                            transport = headers.get("transport", "")
                            if "interleaved=" not in transport:
                                write(
                                    f"RTSP/1.0 461 Unsupported Transport\r\nCSeq: {cseq}\r\n\r\n".encode()
                                )
                                continue
                            first = int(transport.split("interleaved=")[1].split("-")[0])
                            kinds = server.tracks(path)
                            channels[kinds[min(int(track or 1), len(kinds)) - 1]] = first
                            lease = (
                                f";timeout={server.session_timeout}"
                                if server.session_timeout
                                else ""
                            )
                            extra = (
                                f"Transport: RTP/AVP/TCP;unicast;interleaved={first}-{first + 1}\r\n"
                                f"Session: 1A2B3C{lease}\r\n"
                            )
                        elif method == "PLAY":
                            frames = [] if server.silent else server.frames
                            session = _Session(
                                path,
                                frames,
                                write,
                                dict(channels),
                                server.session_timeout,
                                expire,
                            )
                            extra = "Session: 1A2B3C\r\nRange: npt=0.000-\r\n"
                        elif method == "TEARDOWN" and session:
                            session.stop.set()
                        elif method not in ("GET_PARAMETER", "DESCRIBE"):
                            write(
                                f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n".encode()
                            )
                            continue
                        if method == "DESCRIBE" and not server.frames:
                            write(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
                            continue
                        length = f"Content-Length: {len(body.encode())}\r\n" if body else ""
                        write(
                            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}{length}\r\n{body}".encode()
                        )
                        if method == "PLAY" and session:
                            threading.Thread(target=session.run, daemon=True).start()
                except (OSError, ValueError):
                    pass
                finally:
                    if session:
                        session.stop.set()

        return Handler
