import hashlib
import socketserver
import struct
import threading
import time

import pytest

from qa_tool.core.taboo import TabooViolation, check_rtsp
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, RtspError, WatchEnd, parse_sdp
from tests import mock_rtsp
from tests.mock_rtsp import MockRtspServer

needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


@pytest.mark.parametrize("method", ["SET_PARAMETER", "ANNOUNCE", "RECORD", "REDIRECT"])
def test_taboo_blocks_changing_methods(method):
    with pytest.raises(TabooViolation):
        check_rtsp(method)
    client = RtspClient("rtsp://127.0.0.1:1/x")
    with pytest.raises(TabooViolation):
        client.request(method, client.url)  # до сети не доходит: сокета ещё нет


def test_credentials_move_from_url_to_header():
    client = RtspClient("rtsp://ad%40min:p%3Ass@10.0.0.5:8554/live")
    assert client.url == "rtsp://10.0.0.5:8554/live"
    assert (client.user, client.password) == ("ad@min", "p:ss")


def test_parse_sdp_controls():
    sdp = (
        "v=0\r\na=control:*\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
        "a=control:trackID=1\r\nm=application 0 RTP/AVP 107\r\n"
        "a=rtpmap:107 vnd.onvif.metadata/90000\r\na=control:rtsp://h/abs/meta\r\n"
    )
    video, meta = parse_sdp(sdp, "rtsp://h/live/")
    assert (video.media, video.encoding, video.control) == (
        "video",
        "H264",
        "rtsp://h/live/trackID=1",
    )
    assert (meta.encoding, meta.control) == ("vnd.onvif.metadata", "rtsp://h/abs/meta")


class _DigestOnly(socketserver.StreamRequestHandler):
    """Сервер, который на всё отвечает 401, пока Digest (qop=auth) не сойдётся."""

    def handle(self):
        while line := self.rfile.readline():
            method, uri, _ = line.decode().split(" ", 2)
            headers = {}
            while raw := self.rfile.readline().decode().strip():
                key, _, value = raw.partition(":")
                headers[key.lower()] = value.strip()
            auth = headers.get("authorization", "")
            ok = False
            if auth.startswith("Digest"):
                p = dict(x.strip().split("=", 1) for x in auth[7:].split(","))
                p = {k: v.strip('"') for k, v in p.items()}
                ha1 = hashlib.md5(b"admin:cam:secret").hexdigest()
                ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
                want = hashlib.md5(
                    f"{ha1}:n1:{p['nc']}:{p['cnonce']}:auth:{ha2}".encode()
                ).hexdigest()
                ok = p.get("response") == want and p.get("opaque") == "op"
            if ok:
                self.wfile.write(f"RTSP/1.0 200 OK\r\nCSeq: {headers['cseq']}\r\n\r\n".encode())
            else:
                self.wfile.write(
                    f"RTSP/1.0 401 Unauthorized\r\nCSeq: {headers['cseq']}\r\nWWW-Authenticate: "
                    'Digest realm="cam", nonce="n1", qop="auth", opaque="op"\r\n\r\n'.encode()
                )


@pytest.fixture
def digest_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _DigestOnly)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_digest_with_qop(digest_server):
    url = f"rtsp://127.0.0.1:{digest_server}/live"
    with RtspClient(url, "admin", "secret", timeout=3) as client:
        assert client.request("OPTIONS", url).status == 200
    with (
        RtspClient(url, "admin", "wrong", timeout=3) as client,
        pytest.raises(RtspError, match="401"),
    ):
        client.request("OPTIONS", url)


@needs_video
def test_reads_metadata_track_on_requested_channel():
    server = MockRtspServer().start()
    try:
        with RtspClient(f"rtsp://127.0.0.1:{server.port}/live", timeout=3) as client:
            _, tracks = client.describe()
            assert [t.encoding for t in tracks] == ["H264", "vnd.onvif.metadata"]
            client.setup(tracks[1], channel=4)
            client.play()
            packets = client.read_packets(1.2)
        assert packets and {p.channel for p in packets} == {4}
        assert sum(p.marker for p in packets) >= 2  # документ из двух пакетов, маркер — на втором
        assert "SETUP /live" in server.requests and "TEARDOWN /live" in server.requests
    finally:
        server.stop()


class _Replies(socketserver.StreamRequestHandler):
    """Сервер, который на любой запрос шлёт заранее заданные байты и закрывает соединение."""

    reply = b""

    def handle(self):
        self.rfile.readline()
        self.wfile.write(self.reply)


def _serve(reply: bytes):
    handler = type("H", (_Replies,), {"reply": reply})
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server


@pytest.mark.parametrize(
    "reply",
    [
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 99999999\r\n\r\n",
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: -5\r\n\r\n",
        b"$\x00\x10",  # обрыв посреди пакета RTP
    ],
)
def test_hostile_or_broken_replies_raise_rtsp_error(reply):
    server = _serve(reply)
    try:
        client = RtspClient(f"rtsp://127.0.0.1:{server.server_address[1]}/x", timeout=3)
        with client, pytest.raises(RtspError):
            client.request("OPTIONS", "*")
    finally:
        server.server_close()


def _playing(server: MockRtspServer, path: str = "/live") -> RtspClient:
    """Клиент с запущенным видео; закрывать — вызывающему."""
    client = RtspClient(f"rtsp://127.0.0.1:{server.port}{path}", timeout=3)
    client.connect()
    _, tracks = client.describe()
    client.setup(next(t for t in tracks if t.media == "video"))
    client.play()
    return client


@needs_video
def test_watch_hands_over_numbered_packets_and_keeps_nothing():
    server = MockRtspServer(metadata=False).start()
    packets: list[RtpPacket] = []
    try:
        client = _playing(server)
        assert client.watch(packets.append, 1.0) is WatchEnd.TIME_UP
        assert len(client._buf) < 1 << 20
        client.close()
    finally:
        server.stop()
    numbers = [p.sequence for p in packets]
    assert len(numbers) > 10
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    moments = [p.arrived for p in packets]
    assert moments == sorted(moments) and moments[-1] > moments[0]


@needs_video
def test_watch_stops_on_request_while_stream_flows():
    server = MockRtspServer(metadata=False).start()
    try:
        client = _playing(server)
        started = time.monotonic()
        end = client.watch(lambda p: None, 60, stop=lambda: time.monotonic() - started > 0.3)
        assert end is WatchEnd.STOPPED
        assert time.monotonic() - started < 5
        client.close()
    finally:
        server.stop()


@needs_video
def test_watch_stops_on_request_on_silent_camera():
    server = MockRtspServer(metadata=False, silent=True).start()
    try:
        client = _playing(server)
        started = time.monotonic()
        end = client.watch(lambda p: None, 60, stop=lambda: time.monotonic() - started > 0.3)
        assert end is WatchEnd.STOPPED
        assert time.monotonic() - started < 5
        client.close()
    finally:
        server.stop()


@needs_video
def test_watch_reports_silence_and_session_stays_usable():
    server = MockRtspServer(metadata=False, silent=True).start()
    try:
        client = _playing(server)
        assert client.watch(lambda p: None, 60, silence_s=0.6) is WatchEnd.SILENT
        assert client.request("OPTIONS", client.url).status == 200  # тайм-аут не портит соединение
        client.close()
    finally:
        server.stop()


def test_watch_reports_broken_connection():
    server = _serve(b"$\x00\x10")
    try:
        client = RtspClient(f"rtsp://127.0.0.1:{server.server_address[1]}/x", timeout=3)
        with client:
            client._sock.sendall(b"PING\r\n")
            assert client.watch(lambda p: None, 5) is WatchEnd.BROKEN
    finally:
        server.server_close()


def test_frame_split_across_reads_is_assembled():
    packet = struct.pack("!BBHII", 0x80, 0xE0, 7, 9000, 1) + b"abc"
    frame = b"$\x00" + struct.pack("!H", len(packet)) + packet
    client = RtspClient("rtsp://127.0.0.1:1/x")
    client._buf += frame[:6]
    assert client._take_frame() is None
    client._buf += frame[6:]
    taken = client._take_frame()
    assert (taken.sequence, taken.timestamp, taken.marker, taken.payload) == (7, 9000, True, b"abc")
    assert not client._buf


def test_answer_split_across_reads_is_assembled():
    answer = b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 3\r\n\r\nabc"
    client = RtspClient("rtsp://127.0.0.1:1/x")
    client._buf += answer[:-2]
    assert client._take_frame() is None
    client._buf += answer[-2:]
    taken = client._take_frame()
    assert (taken.status, taken.body) == (200, "abc")


def test_garbage_instead_of_answer_raises():
    client = RtspClient("rtsp://127.0.0.1:1/x")
    client._buf += b"HTTP-ish garbage\r\n"
    with pytest.raises(RtspError, match="не ответ RTSP"):
        client._take_frame()


def test_endless_header_raises():
    client = RtspClient("rtsp://127.0.0.1:1/x")
    client._buf += b"RTSP/1.0 200 OK\r\nX: " + b"a" * 2_000_000
    with pytest.raises(RtspError):
        client._take_frame()


def _numbers_after_watch(server: MockRtspServer, seconds: float) -> tuple[list[int], WatchEnd]:
    packets: list[RtpPacket] = []
    client = _playing(server)
    end = client.watch(packets.append, seconds)
    client.close()
    return [p.sequence for p in packets], end


@needs_video
def test_session_dies_without_keepalive_on_mock():
    """Сам mock закрывает сессию без удержания: иначе тесты ниже ничего бы не доказывали."""
    server = MockRtspServer(metadata=False, session_timeout=1).start()
    try:
        client = _playing(server)
        assert client.read_packets(3, limit=1_000_000)
        assert server.expired
        client.close()
    finally:
        server.stop()


@needs_video
def test_keepalive_holds_session_and_loses_no_packets():
    server = MockRtspServer(metadata=False, session_timeout=1, ping_delay_s=0.2).start()
    try:
        numbers, end = _numbers_after_watch(server, 2.5)
    finally:
        server.stop()
    assert end is WatchEnd.TIME_UP
    assert not server.expired
    assert server.requests.count("GET_PARAMETER /live") >= 3
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))


@needs_video
def test_camera_silent_on_keepalive_does_not_stop_reading():
    server = MockRtspServer(metadata=False, session_timeout=1, ping="silent").start()
    try:
        numbers, end = _numbers_after_watch(server, 2.5)
    finally:
        server.stop()
    assert end is WatchEnd.TIME_UP
    assert len(numbers) > 30


@needs_video
def test_keepalive_falls_back_to_options_when_refused():
    server = MockRtspServer(metadata=False, session_timeout=1, ping="refused").start()
    try:
        _, end = _numbers_after_watch(server, 2.5)
    finally:
        server.stop()
    assert end is WatchEnd.TIME_UP
    assert not server.expired
    assert server.requests.count("GET_PARAMETER /live") == 1
    assert server.requests.count("OPTIONS /live") >= 2


def test_keepalive_goes_through_taboo(monkeypatch):
    seen = []
    monkeypatch.setattr("qa_tool.drivers.camera.rtsp.check_rtsp", seen.append)

    class Sink:
        def sendall(self, data):
            pass

    client = RtspClient("rtsp://127.0.0.1:1/x")
    client._sock = Sink()
    client._ping()
    client._sock = None
    assert seen == ["GET_PARAMETER"]
