import time

import pytest

from qa_tool.core.models import Camera
from qa_tool.drivers.camera.discovery import discover
from qa_tool.drivers.camera.onvif_generic import OnvifCamera, stand_service_url
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, credentials_rejected
from tests.mock_camera import start_fleet


@pytest.fixture
def fleet():
    cameras, responder = start_fleet(8, with_discovery=True)
    yield cameras
    responder.stop()
    for cam in cameras:
        cam.stop()


def as_camera(mock, slot=1):
    return Camera(slot=slot, address="127.0.0.1", port=mock.port, username="admin")


def test_passport_and_capabilities(fleet):
    driver = OnvifCamera(as_camera(fleet[0]), "admin")
    passport = driver.get_device_information()
    assert (passport.manufacturer, passport.model, passport.serial) == ("Ruver", "MOCK-1", "SN0001")
    caps = driver.get_capabilities()
    assert caps["Media"].endswith("/onvif/media_service")


def test_exchange_recorded_without_password_digest(fleet):
    driver = OnvifCamera(as_camera(fleet[0]), "admin")
    driver.get_device_information()
    exchange = driver.exchanges[-1]
    assert exchange.status == 200
    assert "<wsse:Password" in exchange.request
    assert "***</wsse:Password>" in exchange.request


def test_wrong_password_raises_fault(fleet):
    with pytest.raises(OnvifFault, match="NotAuthorized"):
        OnvifCamera(as_camera(fleet[0]), "wrong").get_device_information()


def test_unreachable_camera():
    camera = Camera(slot=1, address="127.0.0.1", port=1, username="admin")
    with pytest.raises(CameraUnreachable):
        OnvifCamera(camera, "admin", timeout=1).get_device_information()


@pytest.mark.parametrize(
    "how", [{"interface": "127.0.0.1"}, {"target": "127.0.0.1"}], ids=["multicast", "unicast"]
)
def test_discovery_finds_all_mock_cameras_within_10s(fleet, how):
    start = time.monotonic()
    found = discover(timeout=2, **how)
    assert time.monotonic() - start <= 10
    ports = {c.port for c in found if c.address == "127.0.0.1"}
    assert {m.port for m in fleet} <= ports  # в сети могут быть и другие ONVIF-устройства
    first = next(c for c in found if c.port == fleet[0].port)
    assert (first.manufacturer, first.model) == ("Ruver", "MOCK-1")


def test_garbage_probe_matches_are_skipped():
    from qa_tool.drivers.camera.discovery import parse_probe_matches

    bad_port = (
        b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        b'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches>'
        b"<d:ProbeMatch><d:XAddrs>http://10.0.0.5:99999/onvif</d:XAddrs></d:ProbeMatch>"
        b"<d:ProbeMatch><d:XAddrs>http://10.0.0.6/onvif</d:XAddrs></d:ProbeMatch>"
        b"</d:ProbeMatches></s:Body></s:Envelope>"
    )
    assert [c.address for c in parse_probe_matches(bad_port)] == ["10.0.0.6"]
    dtd = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'
    with pytest.raises(ValueError):  # discover() пропускает такие пакеты (ловит ValueError)
        parse_probe_matches(dtd)


def test_username_with_xml_characters_is_escaped():
    from qa_tool.drivers.camera.soap import SoapClient

    header = SoapClient("a&b<c", "pw")._security_header("http://камера/")
    assert "<wsse:Username>a&amp;b&lt;c</wsse:Username>" in header


@pytest.mark.parametrize(
    ("fault", "rejected"),
    [
        (OnvifFault("x", 400, "ter:NotAuthorized"), True),
        (OnvifFault("x", 400, "wsse:FailedAuthentication"), True),
        (OnvifFault("ответ не SOAP (HTTP 401)", status=401), True),
        (OnvifFault("x", 401, "ter:Other"), True),
        (OnvifFault("ter:ActionNotSupported NotAuthorized HTTP 401", 500, "ter:Action"), False),
        (OnvifFault("ответ не SOAP (HTTP 500)", status=500), False),
        (OnvifFault("камера не объявляет службу"), False),
    ],
)
def test_only_codes_decide_that_credentials_were_rejected(fault, rejected):
    assert credentials_rejected(fault) is rejected


TUNNEL = Camera(slot=1, address="127.0.0.1", port=9114, username="a", lan_address="192.168.1.114")
DIRECT = Camera(slot=1, address="192.168.1.10", port=80, username="a")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.168.1.114/onvif/Replay", "http://127.0.0.1:9114/onvif/Replay"),
        ("http://192.168.1.114:80/onvif/a?b=1", "http://127.0.0.1:9114/onvif/a?b=1"),
        ("http://127.0.0.1/onvif/Replay", "http://127.0.0.1:9114/onvif/Replay"),  # Dahua
        ("http://10.9.9.9/onvif/Replay", None),
        ("http://[1:2]/onvif/Replay", None),
    ],
)
def test_service_of_camera_behind_tunnel(url, expected):
    assert stand_service_url(TUNNEL, url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.168.1.10:8000/onvif/a", "http://192.168.1.10:8000/onvif/a"),
        ("http://192.168.1.114/onvif/a", None),
    ],
)
def test_direct_camera_keeps_addresses_and_has_no_aliases(url, expected):
    assert stand_service_url(DIRECT, url) == expected
