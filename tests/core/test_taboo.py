import httpx
import pytest

from qa_tool.core.checks import CheckContext, Registry, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.core.taboo import FORBIDDEN_ONVIF, TabooViolation, check_http, check_onvif
from qa_tool.drivers.camera.soap import SoapClient
from tests.mock_camera import start_fleet


@pytest.mark.parametrize(
    "body",
    [
        "<tds:SetNetworkInterfaces><tds:InterfaceToken>eth0</tds:InterfaceToken></tds:SetNetworkInterfaces>",
        "<tds:SetDNS/>",
        "<tds:SetNetworkDefaultGateway/>",
        "<tds:SetNetworkProtocols/>",
        "<tds:CreateUsers/>",
        "<tds:SetUser><tt:Username>admin</tt:Username></tds:SetUser>",
        "<tds:DeleteUsers/>",
        "<tds:SetSystemFactoryDefault><tds:FactoryDefault>Soft</tds:FactoryDefault></tds:SetSystemFactoryDefault>",
        "<SetHostname xmlns='http://www.onvif.org/ver10/device/wsdl'/>",
    ],
)
def test_forbidden_onvif_operations_rejected(body):
    with pytest.raises(TabooViolation, match="запрещена"):
        check_onvif(body)


@pytest.mark.parametrize(
    "body",
    [
        "<tds:GetNetworkInterfaces/>",
        "<tds:GetUsers/>",
        "<tds:GetDeviceInformation/>",
        "<tds:SystemReboot/>",
    ],
)
def test_reading_and_reboot_allowed(body):
    check_onvif(body)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/network/tcpip/ip"),
        ("POST", "/api/network/ports/rtsp"),
        ("post", "/API/Network/tcpip/apply?x=1"),
        ("POST", "/api/users/set_password"),
        ("POST", "/api/users/add_user"),
        ("POST", "/api/snmp/edit_account"),
        ("POST", "/api/lte/apn"),
        ("POST", "/api/service/reset"),
        ("POST", "/api/service/import_settings"),
        ("DELETE", "/api/users"),
    ],
)
def test_forbidden_api_writes_rejected(method, path):
    with pytest.raises(TabooViolation, match="запрещён"):
        check_http(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/network/tcpip"),
        ("GET", "/api/users"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/image/brightness"),
        ("POST", "/api/service/reboot"),
        ("POST", "/api/networkx/foo"),
    ],
)
def test_reading_and_other_writes_allowed(method, path):
    check_http(method, path)


def test_soap_client_does_not_send_forbidden_request(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append(a))
    client = SoapClient("admin", "admin")
    with pytest.raises(TabooViolation):
        client.call("http://127.0.0.1:1/onvif/device_service", "<tds:SetNetworkInterfaces/>")
    assert sent == [] and client.exchanges == []


def test_check_breaking_taboo_is_stand_error_and_camera_untouched():
    cameras, _ = start_fleet(1)
    mock = cameras[0]
    try:
        reg = Registry()

        def bad_check(ctx):
            SoapClient("admin", ctx.password).call(mock.xaddr, "<tds:SetUser/>")

        reg.check("T-BAD", "core", "", "")(bad_check)
        camera = Camera(slot=1, address="127.0.0.1", port=mock.port, username="admin")
        result = run_check(reg.get("T-BAD"), CheckContext(camera, "admin", Place.COLOR_BOX))
        assert result.status is Status.STAND_ERROR
        assert "запрещённая операция не отправлена" in result.reason
        assert mock.requests == []
    finally:
        mock.stop()


def test_forbidden_list_covers_network_and_accounts():
    assert {"setnetworkinterfaces", "setdns", "createusers", "setuser"} <= FORBIDDEN_ONVIF
