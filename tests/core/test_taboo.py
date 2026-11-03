import httpx
import pytest

from qa_tool.core.checks import CheckContext, Registry, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.core.taboo import (
    FORBIDDEN_ONVIF,
    TabooViolation,
    check_http,
    check_onvif,
    check_web_request,
)
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


@pytest.mark.parametrize(
    ("method", "url", "body", "title"),
    [
        ("POST", "http://cam/ISAPI/System/Network/interfaces/1", "", ""),  # Hikvision
        (
            "PUT",
            "http://cam/cgi-bin/configManager.cgi?action=setConfig&Network.eth0.IPAddress=1",
            "",
            "",
        ),
        ("POST", "http://cam/axis-cgi/pwdgrp.cgi", "action=update&user=root", ""),  # Axis
        ("POST", "http://cam/LAPI/V1.0/Channels/0/System/Users", "", ""),  # Uniview
        ("POST", "http://cam/api/save", '{"dhcp": false}', ""),
        ("POST", "http://cam/api/save", "", "Настройки сети"),
        ("POST", "http://cam/api/save", "", "Пользователи"),
        ("POST", "http://cam/cgi-bin/magicBox.cgi?action=reboot&factory=1", "", ""),
        ("POST", "http://cam/api/firmware/upload", "", ""),
    ],
)
def test_web_crawler_write_requests_on_risky_pages_blocked(method, url, body, title):
    with pytest.raises(TabooViolation, match="не отправлен"):
        check_web_request(method, url, body, title)


@pytest.mark.parametrize(
    ("method", "url", "body", "title"),
    [
        ("GET", "http://cam/ISAPI/System/Network/interfaces/1", "", ""),
        (
            "POST",
            "http://cam/ISAPI/Image/channels/1/color",
            "<brightness>60</brightness>",
            "Изображение",
        ),
        ("POST", "http://cam/api/image/brightness", '{"value": 55}', "Видео"),
    ],
)
def test_web_crawler_reads_and_image_settings_allowed(method, url, body, title):
    check_web_request(method, url, body, title)


# --- находки ревью skeleton (W1–W4, N1) -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "http://cam/cgi-bin/configManager.cgi?action=setConfig&Network.eth0.IPAddress=1"),
        ("GET", "http://cam/axis-cgi/param.cgi?action=update&root.Network.eth0.IPAddress=1"),
        ("GET", "http://cam/axis-cgi/pwdgrp.cgi?action=update&user=root&pwd=x"),
        ("GET", "http://cam/cgi-bin/userManager.cgi?action=modifyPassword&name=admin"),
        ("PUT", "http://cam/ISAPI/System/configurationData"),
        ("PUT", "http://cam/ISAPI/System/activate"),
        ("GET", "http://cam/cgi-bin/magicBox.cgi?action=resetSystemEx&type=1"),
    ],
)
def test_vendor_write_requests_blocked_even_as_get(method, url):
    with pytest.raises(TabooViolation):
        check_web_request(method, url)


@pytest.mark.parametrize(
    "url",
    [
        "http://cam/cgi-bin/configManager.cgi?action=getConfig&name=Network",
        "http://cam/ISAPI/System/Network/interfaces",
        "http://cam/axis-cgi/param.cgi?action=list&group=Network",
    ],
)
def test_vendor_reads_allowed(url):
    check_web_request("GET", url)


@pytest.mark.parametrize(
    "path",
    [
        "http://cam:80/api/network/tcpip/ip",
        "/api//network/tcpip/ip",
        "/api/./network/tcpip/ip",
        "/api/image/../network/tcpip/ip",
        "/api/%6Eetwork/tcpip/ip",
        "/api/%256Eetwork/tcpip/ip",
        "/API/Users/set_password?x=1",
        "\\api\\users\\add_user",
    ],
)
def test_check_http_bypasses_closed(path):
    with pytest.raises(TabooViolation):
        check_http("POST", path)


@pytest.mark.parametrize(
    "body",
    [
        "<tds:SetHashingAlgorithm/>",
        "<tds:SetPasswordComplexityConfiguration/>",
        "<tds:LoadCertificates/>",
        "<tas:CreateRSAKeyPair/>",
        "<!-- <tds:GetUsers/> --><tds:SetUser/>",
        "<tds:GetDeviceInformation/><tds:SetDNS/>",
        '<s:Envelope xmlns:s="x"><s:Header><wsse:Security/></s:Header>'
        "<s:Body><tds:SetNetworkInterfaces/></s:Body></s:Envelope>",
    ],
)
def test_hidden_or_new_onvif_operations_blocked(body):
    with pytest.raises(TabooViolation):
        check_onvif(body)


@pytest.mark.parametrize(
    ("url", "body", "title"),
    [
        ("http://cam/api/image/ircut_filter", '{"mode": "auto"}', "Фильтр ИК"),
        ("http://cam/api/report/generate", "", "Отчёт"),
        ("http://cam/api/video/export_snapshot", "", "Экспорт кадра"),
        ("https://cam/api/image/brightness", '{"value": 40}', "Изображение"),
    ],
)
def test_no_false_positives_on_image_and_report(url, body, title):
    check_web_request("POST", url, body, title)


def test_single_http_exit_blocks_before_network(monkeypatch):
    from qa_tool.drivers.camera import http

    sent = []
    monkeypatch.setattr(httpx, "request", lambda *a, **k: sent.append(a))
    with pytest.raises(TabooViolation):
        http.request("POST", "http://cam/api/users/set_password", content=b'{"p": "x"}')
    with pytest.raises(TabooViolation):
        http.request(
            "POST",
            "http://cam/onvif/device_service",
            content=b"<s:Envelope><tds:SetUser/>",
            headers={"Content-Type": "application/soap+xml"},
        )
    assert sent == []
    http.request("POST", "http://cam/api/image/brightness", content=b'{"value": 40}')
    assert len(sent) == 1
