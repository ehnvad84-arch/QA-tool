"""Мультикаст в Set*Configuration — сетевая настройка (решение директора 08.11, AD-7)."""

import httpx
import pytest

from qa_tool.core.taboo import MulticastGuard, TabooViolation
from qa_tool.drivers.camera import http

NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema"'
)


def multicast(address="239.0.0.1", port=5000):
    return (
        "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
        f"<tt:IPv4Address>{address}</tt:IPv4Address></tt:Address>"
        f"<tt:Port>{port}</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart>"
        "</tt:Multicast>"
    )


def configuration(token="ve-1", extra="", bitrate=4096):
    return (
        f'<tr2:Configurations token="{token}"><tt:Name>Кодировщик</tt:Name>'
        f"<tt:Encoding>H264</tt:Encoding><tt:RateControl><tt:BitrateLimit>{bitrate}"
        f"</tt:BitrateLimit></tt:RateControl>{extra}</tr2:Configurations>"
    )


def answer(*configs):
    return (
        f"<s:Envelope {NS}><s:Body><tr2:GetVideoEncoderConfigurationsResponse>"
        f"{''.join(configs)}</tr2:GetVideoEncoderConfigurationsResponse></s:Body></s:Envelope>"
    )


def set_request(config):
    config = config.replace("tr2:Configurations", "tr2:Configuration")
    return (
        f"<s:Envelope {NS}><s:Body><tr2:SetVideoEncoderConfiguration>{config}"
        "</tr2:SetVideoEncoderConfiguration></s:Body></s:Envelope>"
    )


def test_same_multicast_as_camera_gave_goes_through():
    guard = MulticastGuard()
    guard.remember("cam:80", answer(configuration(extra=multicast())))
    guard.check("cam:80", set_request(configuration(extra=multicast(), bitrate=1024)))


@pytest.mark.parametrize("changed", [multicast("239.9.9.9"), multicast(port=6000)])
def test_changed_multicast_is_refused(changed):
    guard = MulticastGuard()
    guard.remember("cam:80", answer(configuration(extra=multicast())))
    with pytest.raises(TabooViolation, match="меняет мультикаст"):
        guard.check("cam:80", set_request(configuration(extra=changed)))


def test_configuration_never_read_is_refused():
    guard = MulticastGuard()
    with pytest.raises(TabooViolation, match="не читали"):
        guard.check("cam:80", set_request(configuration(extra=multicast())))
    guard.remember("other:80", answer(configuration(extra=multicast())))
    with pytest.raises(TabooViolation, match="не читали"):  # чужая камера не в счёт
        guard.check("cam:80", set_request(configuration(extra=multicast())))


def test_set_without_multicast_and_reads_are_not_touched():
    guard = MulticastGuard()
    guard.check("cam:80", set_request(configuration()))
    guard.check("cam:80", answer(configuration(extra=multicast())))  # Get*, не Set


def test_unparsable_set_with_multicast_is_refused():
    guard = MulticastGuard()
    broken = set_request(configuration(extra=multicast())).replace("</s:Body>", "")
    with pytest.raises(TabooViolation, match="не разобран"):
        guard.check("cam:80", broken)


def test_http_exit_point_remembers_answers_and_refuses_changes(monkeypatch):
    sent = []

    def fake(method, url, **kwargs):
        sent.append(kwargs["content"])
        return httpx.Response(200, text=answer(configuration(extra=multicast())))

    monkeypatch.setattr(http, "_MULTICAST", MulticastGuard())
    monkeypatch.setattr(http.httpx, "request", fake)
    url = "http://10.0.0.5/onvif/media2_service"
    with pytest.raises(TabooViolation):
        http.request("POST", url, content=set_request(configuration(extra=multicast())))
    assert sent == []  # не читали — запрос не ушёл
    http.request("POST", url, content="<tr2:GetVideoEncoderConfigurations/>")
    http.request("POST", url, content=set_request(configuration(extra=multicast(), bitrate=512)))
    with pytest.raises(TabooViolation):
        http.request("POST", url, content=set_request(configuration(extra=multicast("239.1.1.1"))))
    assert len(sent) == 2
