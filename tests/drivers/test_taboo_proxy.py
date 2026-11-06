import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from qa_tool.drivers.camera.taboo_proxy import TabooProxy
from tests.mock_camera import start_fleet
from tests.test_mock_camera import soap


@pytest.fixture
def mock():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


@pytest.fixture
def proxy(mock):
    p = TabooProxy("127.0.0.1", mock.port).start()
    yield p
    p.stop()


def post(proxy, body: bytes, path="/onvif/device_service"):
    return httpx.post(
        proxy.base_url + path,
        content=body,
        headers={"Content-Type": "application/soap+xml"},
        timeout=10,
    )


def test_forbidden_operation_never_reaches_camera(proxy, mock):
    r = post(proxy, soap("SetUser"))
    assert r.status_code == 400 and "QAToolTaboo" in r.text
    assert "SetUser" not in mock.requests
    assert proxy.blocked and "SetUser" in proxy.blocked[0].reason


def test_allowed_request_forwarded_with_auth(proxy, mock):
    r = post(proxy, soap("GetDeviceInformation"))
    assert r.status_code == 200 and "SN0001" in r.text
    assert mock.requests == ["GetDeviceInformation"]


def test_capabilities_addresses_point_to_proxy(proxy, mock):
    r = post(proxy, soap("GetCapabilities"))
    assert f"{proxy.base_url}/_port/{mock.port}/onvif/media_service" in r.text
    assert f"127.0.0.1:{mock.port}/onvif" not in r.text.replace(f"_port/{mock.port}", "")


def test_rewrite_services_subscriptions_and_foreign_hosts():
    p = TabooProxy("10.0.0.5", 80)
    p._server = type("S", (), {"server_address": ("127.0.0.1", 5555)})()
    xml = (
        '<s:Envelope xmlns:tt="http://www.onvif.org/ver10/schema">'
        "<tds:Service><tds:XAddr>http://10.0.0.5/onvif/media2</tds:XAddr></tds:Service>"
        "<tds:Service><tds:XAddr>http://10.0.0.5:8999/onvif/events</tds:XAddr></tds:Service>"
        "<tev:SubscriptionReference><wsa:Address>http://10.0.0.5:8999/sub/7</wsa:Address>"
        "</tev:SubscriptionReference>"
        "<tt:XAddr>http://192.168.1.99/onvif/ptz</tt:XAddr>"
        "<tt:Uri>rtsp://10.0.0.5:554/main</tt:Uri></s:Envelope>"
    )
    out = p.rewrite(xml)
    assert "http://127.0.0.1:5555/_port/80/onvif/media2" in out
    assert "http://127.0.0.1:5555/_port/8999/onvif/events" in out
    assert "http://127.0.0.1:5555/_port/8999/sub/7" in out
    assert "http://127.0.0.1:5555/_foreign/192.168.1.99/onvif/ptz" in out
    assert 'xmlns:tt="http://www.onvif.org/ver10/schema"' in out  # пространства имён не трогаем
    assert "rtsp://10.0.0.5:554/main" in out  # RTSP — к камере напрямую
    assert p.camera_url("/_port/8999/sub/7?x=1") == "http://10.0.0.5:8999/sub/7?x=1"
    assert p.camera_url("/onvif/device_service") == "http://10.0.0.5:80/onvif/device_service"
    assert p.camera_url("/_foreign/192.168.1.99/onvif/ptz") is None


@pytest.mark.parametrize("scheme", ["HTTP", "Http", "hTTp"])
def test_uppercase_scheme_still_goes_through_proxy(scheme):
    p = TabooProxy("10.0.0.5", 80)
    p._server = type("S", (), {"server_address": ("127.0.0.1", 5555)})()
    out = p.rewrite(f"<tds:XAddr>{scheme}://10.0.0.5/onvif/media2</tds:XAddr>")
    assert out == "<tds:XAddr>http://127.0.0.1:5555/_port/80/onvif/media2</tds:XAddr>"
    assert "_port/443/" in p.rewrite("<tt:XAddr>HTTPS://10.0.0.5/x</tt:XAddr>")


def test_path_without_leading_slash_is_not_glued_to_camera_host():
    p = TabooProxy("10.0.0.5", 80)
    assert p.camera_url("@10.9.9.9/x") is None


def test_camera_behind_tunnel_addresses_lead_to_forwarded_port():
    proxy = TabooProxy("127.0.0.1", 9114, lan_address="192.168.1.114").start()
    try:
        out = proxy.rewrite(
            "<tds:XAddr>http://192.168.1.114/onvif/Media</tds:XAddr>"
            "<tds:XAddr>http://127.0.0.1/onvif/x</tds:XAddr>"  # Dahua: хост из запроса, без порта
            "<wsa:Address>http://192.168.1.114:80/onvif/Events/Sub1</wsa:Address>"
            "<tds:XAddr>http://10.9.9.9/onvif/y</tds:XAddr>"
        )
        assert f"{proxy.base_url}/onvif/Media<" in out
        assert f"{proxy.base_url}/onvif/x<" in out
        assert f"{proxy.base_url}/onvif/Events/Sub1<" in out
        assert f"{proxy.base_url}/_foreign/10.9.9.9/onvif/y<" in out
        assert "192.168.1.114" not in out
        assert proxy.camera_url("/onvif/Media") == "http://127.0.0.1:9114/onvif/Media"
    finally:
        proxy.stop()


def test_foreign_host_not_forwarded(proxy):
    r = httpx.post(proxy.base_url + "/_foreign/192.168.1.99/onvif/ptz", content=b"<x/>", timeout=5)
    assert r.status_code == 502 and "QAToolForeignHost" in r.text


def test_long_poll_longer_than_default_http_timeout_passes():
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(6)  # дольше 5 с по умолчанию у http.request, как PullMessages
            body = b"<ok/>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True).start()
    p = TabooProxy("127.0.0.1", server.server_address[1], timeout_s=15).start()
    try:
        r = httpx.post(p.base_url + "/events", content=b"{}", timeout=20)
        assert r.status_code == 200 and r.text == "<ok/>"
    finally:
        p.stop()
        server.shutdown()
        server.server_close()


@pytest.fixture
def answering():
    """Камера с заданным ответом: `answering(тело, заголовки)` — посредник к ней."""
    servers, proxies = [], []

    def start(content: bytes, status=200, **headers):
        class Canned(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key.replace("_", "-"), value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Canned)
        threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True).start()
        servers.append(server)
        proxies.append(TabooProxy("127.0.0.1", server.server_address[1]).start())
        return proxies[-1], server.server_address[1]

    yield start
    for p in proxies:
        p.stop()
    for server in servers:
        server.shutdown()
        server.server_close()


def test_undecodable_answer_in_single_byte_charset_still_reaches_client(answering):
    # 0x98 — единственный байт вне windows-1251: декодируется в U+FFFD, а обратно не кодируется
    proxy, _ = answering(b"<a>\x98</a>", Content_Type="text/xml; charset=windows-1251")
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.status_code == 200 and r.content.startswith(b"<a>")


def test_redirect_to_camera_stays_behind_proxy(answering):
    proxy, _ = answering(b"", status=302, Location="http://127.0.0.1:8080/onvif/x")
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.headers["location"] == f"{proxy.base_url}/_port/8080/onvif/x"


def test_redirect_to_foreign_host_goes_to_refusal(answering):
    proxy, _ = answering(b"", status=302, Location="http://10.9.9.9/onvif/x")
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert "10.9.9.9" in r.headers["location"]
    assert r.headers["location"].startswith(f"{proxy.base_url}/_foreign/")


def test_redirect_without_scheme_stays_behind_proxy(answering):
    proxy, _ = answering(b"", status=302, Location="//127.0.0.1:8080/onvif/x")
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.headers["location"] == f"{proxy.base_url}/_port/8080/onvif/x"


def test_redirect_without_scheme_to_foreign_host_goes_to_refusal(answering):
    proxy, _ = answering(b"", status=302, Location="//10.9.9.9/onvif/x")
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.headers["location"].startswith(f"{proxy.base_url}/_foreign/")


@pytest.mark.parametrize(
    "location",
    ["http://@10.9.9.9/x", "//@10.9.9.9/x", "http://a:b@10.9.9.9/x", "http://cam_1/x", "ftp://x/y"],
)
def test_redirect_tricks_to_foreign_host_go_to_refusal(answering, location):
    proxy, _ = answering(b"", status=307, Location=location)
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.headers["location"].startswith(f"{proxy.base_url}/_foreign/")


@pytest.mark.parametrize("location", ["http://@127.0.0.1:8080/x", "//@127.0.0.1:8080/x"])
def test_redirect_with_empty_user_to_camera_stays_behind_proxy(answering, location):
    proxy, _ = answering(b"", status=307, Location=location)
    r = httpx.get(proxy.base_url + "/x", timeout=10)
    assert r.headers["location"] == f"{proxy.base_url}/_port/8080/x"


def test_relative_redirect_is_left_alone(answering):
    proxy, _ = answering(b"", status=302, Location="/onvif/x")
    assert httpx.get(proxy.base_url + "/x", timeout=10).headers["location"] == "/onvif/x"


def test_proxy_address_before_at_sign_is_not_the_proxy(answering):
    proxy, _ = answering(b"")
    location = f"http://127.0.0.1:{proxy.port}@10.9.9.9/x"
    assert proxy.rewrite_location(location).startswith(f"{proxy.base_url}/_foreign/")


@pytest.mark.parametrize("text", ["(http://127.0.0.1:8080/x)", "<a href=http://127.0.0.1:8080/x>"])
def test_camera_address_next_to_punctuation_is_rewritten(answering, text):
    proxy, _ = answering(b"")
    assert f"{proxy.base_url}/_port/8080/x" in proxy.rewrite(text)
