"""Чтение родного API (api-discovery, T5): только GET по шаблонам сборщика, табу, пределы,
ошибки разделов, отказ во входе, выбор сборщика по паспорту; разбор «ключ=значение»."""

import xml.etree.ElementTree as ET

import httpx
import pytest

from qa_tool.core.api_map import NativeMap, Section, Sensitivity
from qa_tool.core.models import Camera
from qa_tool.drivers.camera import http, native
from qa_tool.drivers.camera.http import HttpLogin, LoginRefused
from qa_tool.drivers.camera.key_value import (
    is_error_reply,
    parse_key_value,
    table_of,
    without_indexes,
)
from qa_tool.drivers.camera.native import (
    NativeReader,
    NativeUnreachable,
    ParseRefused,
    ReadTemplate,
    choose_collector,
    maker_word,
)
from tests.mock_native import hikvision

SECRET = "test-secret-3"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
CAMERA = Camera(slot=1, address="127.0.0.1", username="admin", port=9100, vendor="hikvision")


class FakeCollector:
    """Сборщик для проверки читателя: шаблоны задаёт тест, разбор — листья XML."""

    brand, name, version, login = "hikvision", "ISAPI", 1, "digest"
    names = frozenset({"hikvision", "hik"})

    def __init__(self, first, then=(), refuse=()):
        self.first, self.then, self.refuse = list(first), list(then), set(refuse)
        self.describes: dict[str, bytes | None] = {}

    def reads(self, found):
        return self.first + (self.then if found else [])

    def parse(self, template, body, content_type, describe):
        if template.section_id in self.refuse:
            raise ParseRefused("не XML")
        self.describes[template.section_id] = describe
        section = Section(template.section_id, template.cls, "", sensitivity=template.sensitivity)
        for element in ET.fromstring(body).iter():
            if len(element) == 0 and element.text:
                section.add_param(
                    f"{template.section_id}/{element.tag.split('}')[-1]}",
                    "строка",
                    None,
                    element.text,
                )
        return section

    def expected(self, kind):
        return ()

    def observed(self, native_map):
        return []

    def restore_request(self, param, m0):
        return None


DEVICE = ReadTemplate("/ISAPI/System/deviceInfo", "System/deviceInfo", "система")
IMAGE = ReadTemplate(
    "/ISAPI/Image/channels/1",
    "Image/channels/1",
    "картинка",
    describe_path="/ISAPI/Image/channels/1/capabilities",
)
AUDIO = ReadTemplate("/ISAPI/System/Audio/channels/1", "System/Audio", "прочее")
NETWORK = ReadTemplate(
    "/ISAPI/System/Network/interfaces/1", "System/Network/interfaces/1", "сеть и безопасность"
)


@pytest.fixture
def camera(monkeypatch):
    def put(**kwargs):
        api = hikvision(password=SECRET, **kwargs)
        monkeypatch.setattr(http, "_TRANSPORT", api.transport())
        return api

    return put


def reader(collector, **kwargs) -> NativeReader:
    return NativeReader(
        CAMERA, collector, HttpLogin("admin", kwargs.pop("password", SECRET)), **kwargs
    )


def meta() -> NativeMap:
    return NativeMap(brand="hikvision", collector="ISAPI", run="QA-1", place=1)


def test_reads_until_collector_has_nothing_new(camera):
    api = camera()
    collector = FakeCollector([DEVICE], then=[IMAGE, AUDIO])
    result = reader(collector).read(meta())
    assert [s.id for s in result.sections] == ["System/deviceInfo", "Image/channels/1"]
    assert result.params()["Image/channels/1/brightnessLevel"].value == "50"
    assert collector.describes["Image/channels/1"].startswith(b"<?xml")
    image = result.sections[1]
    assert image.read == "GET /ISAPI/Image/channels/1" and image.code == 200 and image.size > 0
    assert image.describe == "GET /ISAPI/Image/channels/1/capabilities"
    assert [(e.code, e.reason) for e in result.errors] == [(404, "раздела нет у модели")]
    assert result.login == "Digest MD5"
    # каждый шаблон — один раз, хотя сборщик повторяет его в каждом проходе
    assert sum("deviceInfo" in r for r in api.requests) == 2  # 401 с запросом входа + подписанный


@pytest.mark.parametrize(
    ("template", "reason"),
    [
        (
            ReadTemplate("/cgi-bin/magicBox.cgi", "x", "система", query="action=reboot"),
            "не отправлен",
        ),
        (ReadTemplate("/ISAPI/System/factoryReset", "x", "система"), "не отправлен"),
        (ReadTemplate("/ISAPI/System/configurationData", "x", "система"), "не отправлен"),
        (ReadTemplate("//evil.example/ISAPI", "x", "прочее"), "адрес шаблона"),
        (ReadTemplate("/ISAPI/../etc/passwd", "x", "прочее"), "адрес шаблона"),
        (ReadTemplate("/ISAPI/x", "x", "прочее", query="a=1\r\nHost: evil"), "запрос шаблона"),
        (ReadTemplate("/ISAPI/Image/channels/1", "x", "картинка", method="PUT"), "только читается"),
    ],
)
def test_dangerous_templates_never_reach_camera(camera, template, reason):
    api = camera()
    result = reader(FakeCollector([template])).read(meta())
    assert result.sections == [] and reason in result.errors[0].reason
    assert api.requests == []


def test_login_refusal_propagates_and_is_remembered(camera):
    api = camera()
    refused = []
    login = HttpLogin("admin", "wrong-3", on_refused=refused.append)
    with pytest.raises(LoginRefused):
        NativeReader(CAMERA, FakeCollector([DEVICE, IMAGE]), login).read(meta())
    assert api.refusals == 1 and refused


def test_unparsed_and_oversized_answers_become_errors(camera):
    api = camera()
    api.overrides["/ISAPI/System/deviceInfo"] = (200, "application/xml", b"x" * (3 << 20))
    collector = FakeCollector([DEVICE, IMAGE], refuse={"Image/channels/1"})
    result = reader(collector).read(meta())
    reasons = [e.reason for e in result.errors]
    assert reasons == ["ответ больше 2 МБ — не разобран", "ответ не разобран: не XML"]
    assert result.sections == []


def test_request_limit_cuts_the_map(camera, monkeypatch):
    camera()
    monkeypatch.setattr(native, "MAX_REQUESTS", 1)
    result = reader(FakeCollector([DEVICE], then=[IMAGE])).read(meta())
    assert [s.id for s in result.sections] == ["System/deviceInfo"]
    assert result.cut and "запросов больше 1" in result.cut_reason


def test_budget_cuts_the_map(camera):
    camera()
    ticks = iter([0.0, 0.0, 500.0, 500.0])
    result = reader(FakeCollector([DEVICE, IMAGE]), clock=lambda: next(ticks)).read(meta())
    assert len(result.sections) == 1 and "не уложилась в 120 с" in result.cut_reason


def test_lost_camera_raises_unreachable(monkeypatch):
    calls = []

    def dead(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("нет связи", request=request)

    monkeypatch.setattr(http, "_TRANSPORT", httpx.MockTransport(dead))
    templates = [ReadTemplate(f"/ISAPI/s{i}", f"s{i}", "прочее") for i in range(10)]
    with pytest.raises(NativeUnreachable, match="не ответила на 4 запросов из 4"):
        reader(FakeCollector(templates)).read(meta())
    assert len(calls) == 8  # один повтор на каждый запрос


def test_exchanges_hide_bodies_of_sensitive_sections(camera):
    camera()
    collector = FakeCollector([IMAGE, NETWORK])
    current = reader(collector)
    result = current.read(meta())
    assert result.sections[1].sensitivity is Sensitivity.SENSITIVE
    image, network = current.exchanges
    assert "brightnessLevel" in image.body
    assert network.body == "" and network.size > 0 and network.code == 200
    assert "127.0.0.1" not in str(result.sections[1].params)


class Named:
    def __init__(self, brand, *names):
        self.brand, self.names = brand, frozenset(names)


@pytest.mark.parametrize(
    ("maker", "vendor", "brand", "note"),
    [
        ("HIKVISION", "generic", "hikvision", ""),
        ("Hikvision Digital Technology", "hikvision", "hikvision", ""),
        ("Dahua", "hikvision", "dahua", "паспорт ONVIF — Dahua"),
        ("", "dahua", "dahua", ""),
        ("Acme", "dahua", "dahua", "сборщика для него нет"),
        ("Acme", "generic", None, ""),
    ],
)
def test_choose_collector_prefers_passport(maker, vendor, brand, note):
    collectors = [Named("hikvision", "hikvision"), Named("dahua", "dahua")]
    camera = Camera(slot=1, address="127.0.0.1", username="admin", vendor=vendor)
    choice = choose_collector(collectors, {"manufacturer": maker}, camera)
    assert (choice.collector.brand if choice.collector else None) == brand
    assert note in choice.note and (bool(note) == bool(choice.note))


def test_maker_word():
    assert maker_word({"manufacturer": "  HIKVISION  "}) == "hikvision"
    assert maker_word(None) == "" and maker_word({}) == ""


def test_key_value_parsing():
    text = (
        "table.VideoColor[0][0].Brightness=50\r\n"
        "table.VideoColor[0][1].Brightness = 51\r\n"
        "\r\n"
        "table.VideoColor[0][0].Brightness=99\r\n"
        "<script>=1\r\n"
        "строка без знака\r\n"
        "table.Encode[0].MainFormat[0].Video.BitRate=4096\r\n"
    )
    found = parse_key_value(text)
    assert found.pairs == {
        "VideoColor[0][0].Brightness": "50",
        "VideoColor[0][1].Brightness": "51",
        "Encode[0].MainFormat[0].Video.BitRate": "4096",
    }
    assert found.skipped == 2 and not found.cut
    assert parse_key_value("a=1\nb=2\nc=3", limit=2).cut
    assert parse_key_value("k" * 129 + "=1").skipped == 1


def test_key_value_helpers():
    assert is_error_reply("Error\r\nBad Request!\r\n") and not is_error_reply("type=IPC")
    assert table_of("VideoColor[0][1].Brightness") == "VideoColor"
    assert table_of("Network.eth0.IPAddress") == "Network"
    assert without_indexes("VideoColor[0][2].Brightness") == "VideoColor[*][*].Brightness"
