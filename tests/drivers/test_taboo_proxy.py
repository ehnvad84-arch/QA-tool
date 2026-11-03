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
