import hashlib
import socketserver
import threading

import pytest

from qa_tool.core.taboo import TabooViolation, check_rtsp
from qa_tool.drivers.camera.rtsp import RtspClient, RtspError, parse_sdp
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
