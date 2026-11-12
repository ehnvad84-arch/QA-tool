"""Сборщик Hikvision ISAPI (api-discovery, T6): разделы и значения, типы по атрибутам
`capabilities`, чувствительное, враждебный XML, номера потоков, возврат подресурса картинки."""

import pytest

from qa_tool.core.api_map import NativeMap, Section, Sensitivity, diff
from qa_tool.core.models import Camera
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.hikvision.collector import HikvisionCollector, leaves
from qa_tool.drivers.camera.http import HttpLogin
from qa_tool.drivers.camera.native import NativeReader, ParseRefused, ReadTemplate
from tests.mock_native import hikvision, onvif_set_imaging, side_effect_like_111

SECRET = "test-secret-4"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
CAMERA = Camera(slot=1, address="127.0.0.1", username="admin", port=9100, vendor="hikvision")
COLLECTOR = HikvisionCollector()
IMAGE = ReadTemplate(
    "/ISAPI/Image/channels/1",
    "Image/channels/1",
    "картинка",
    describe_path="/ISAPI/Image/channels/1/capabilities",
)

# Сокращённый ответ по образцу .114 09.11: идентификаторов камеры в нём нет.
CAPS_114 = """<?xml version="1.0" encoding="UTF-8"?>
<ImageChannel version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<ImageFlip><enabled opt="true,false">false</enabled>
<ImageFlipStyle opt="LEFTRIGHT,UPDOWN,CENTER"></ImageFlipStyle></ImageFlip>
<WDR><mode opt="close,open,auto">close</mode><WDRLevel min="0" max="100">50</WDRLevel></WDR>
<IrcutFilter><IrcutFilterType opt="day,night,auto,schedule">day</IrcutFilterType>
<nightToDayFilterTime min="5" max="120">5</nightToDayFilterTime>
<Schedule><TimeRange><beginTime>06:00:00</beginTime></TimeRange></Schedule></IrcutFilter>
<Shutter><ShutterLevel opt="1/3,1/6,1/12,1/25">1/12</ShutterLevel></Shutter>
<Gain><GainLevel min="0" max="100">0</GainLevel></Gain>
<Dehaze><DehazeLevel min="0.5" max="99.5">50</DehazeLevel></Dehaze>
<Overlay><text max="32">Камера</text></Overlay>
</ImageChannel>""".encode()


@pytest.fixture
def camera(monkeypatch):
    def put(**kwargs):
        api = hikvision(password=SECRET, **kwargs)
        monkeypatch.setattr(http, "_TRANSPORT", api.transport())
        return api

    return put


def read_all() -> NativeMap:
    meta = NativeMap(brand="hikvision", collector="ISAPI")
    return NativeReader(CAMERA, COLLECTOR, HttpLogin("admin", SECRET)).read(meta)


def test_map_of_virtual_hikvision(camera):
    api = camera()
    result = read_all()
    ids = [s.id for s in result.sections]
    assert ids[:5] == [
        "System/deviceInfo",
        "System/capabilities",
        "Image/channels/1",
        "Streaming/channels",
        "System/Video/inputs/channels/1/overlays",
    ]
    assert "Streaming/channels/101" in ids
    params = result.params()
    brightness = params["Image/channels/1/Color/brightnessLevel"]
    assert (brightness.type, brightness.allowed, brightness.value) == (
        "целое",
        {"мин": 0, "макс": 100},
        "50",
    )
    exposure = params["Image/channels/1/Exposure/ExposureType"]
    assert exposure.type == "перечисление" and exposure.allowed == {"значения": ["auto", "manual"]}
    assert params["Streaming/channels/101/Video/constantBitRate"].allowed == {
        "мин": 256,
        "макс": 8192,
    }
    assert (
        params[
            "System/Video/inputs/channels/1/overlays/TextOverlayList/TextOverlay[2]/displayText"
        ].value
        == "Надпись 2"
    )
    # потоков в списке два, 102 у виртуальной камеры нет — ошибка раздела, а не сбой карты
    errors = {e.request: e.reason for e in result.errors}
    assert errors["GET /ISAPI/Streaming/channels/102"] == "раздела нет у модели"
    assert errors["GET /ISAPI/System/Network/capabilities"] == "раздела нет у модели"
    assert not any(r.startswith(("PUT", "POST")) for r in api.requests)


def test_sensitive_values_are_not_in_map(camera):
    camera()
    params = read_all().params()
    for key in (
        "System/deviceInfo/serialNumber",
        "System/deviceInfo/macAddress",
        "System/deviceInfo/deviceName",
        "Streaming/channels/StreamingChannel[1]/Transport/Multicast/destIPAddress",
        "Streaming/channels/StreamingChannel[1]/Transport/Security/certificateType",
    ):
        assert params[key].value is None and params[key].sensitivity is not Sensitivity.NORMAL, key
    assert params["System/deviceInfo/model"].value == "DS-2CD2043G2-I"
    assert params["Streaming/channels/StreamingChannel[1]/Video/videoCodecType"].value == "H.264"


def test_types_from_capabilities_of_114():
    section = COLLECTOR.parse(
        ReadTemplate("/ISAPI/Image/channels/1/capabilities", "Image/channels/1", "картинка"),
        CAPS_114,
        "application/xml",
        None,
    )
    found = {p.id.removeprefix("Image/channels/1/"): p for p in section.params}
    assert found["ImageFlip/enabled"].type == "логический"
    assert found["ImageFlip/ImageFlipStyle"].value is None
    assert found["ImageFlip/ImageFlipStyle"].allowed == {
        "значения": ["LEFTRIGHT", "UPDOWN", "CENTER"]
    }
    assert found["IrcutFilter/nightToDayFilterTime"].allowed == {"мин": 5, "макс": 120}
    assert found["IrcutFilter/Schedule/TimeRange/beginTime"].type == "строка"
    assert found["Shutter/ShutterLevel"].type == "перечисление"
    assert found["Dehaze/DehazeLevel"].type == "число"
    assert found["Overlay/text"].allowed == {"длина_макс": 32}
    assert found["Gain/GainLevel"].value == "0"
    assert found["id"].type == "целое"


def test_settings_value_wins_over_capabilities_text():
    settings = b'<ImageChannel xmlns="x"><Gain><GainLevel>70</GainLevel></Gain></ImageChannel>'
    section = COLLECTOR.parse(IMAGE, settings, "application/xml", CAPS_114)
    gain = next(p for p in section.params if p.id == "Image/channels/1/Gain/GainLevel")
    assert gain.value == "70" and gain.allowed == {"мин": 0, "макс": 100}
    # лист есть только в описании — он в карте со значением из описания
    assert any(p.id == "Image/channels/1/WDR/WDRLevel" for p in section.params)


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
        b"<a>" * 40 + b"1" + b"</a>" * 40,
        b"not xml at all",
        b"",
    ],
)
def test_hostile_xml_is_refused(body):
    with pytest.raises(ParseRefused):
        COLLECTOR.parse(IMAGE, body, "application/xml", None)


def test_leaves_index_repeats_only():
    from defusedxml.ElementTree import fromstring

    tree = leaves(fromstring(b"<r><a><b>1</b><b>2</b></a><c>3</c></r>"))
    assert list(tree) == ["a/b[1]", "a/b[2]", "c"]


def test_stream_ids_only_digits_and_at_most_four():
    listing = Section("Streaming/channels", "кодировщик", "")
    for i, value in enumerate(["101", "102", "x1", "../1", "201", "202", "301"], start=1):
        listing.add_param(f"Streaming/channels/StreamingChannel[{i}]/id", "строка", None, value)
    listing.add_param("Streaming/channels/StreamingChannel[1]/Video/id", "строка", None, "999")
    templates = COLLECTOR.reads([listing])
    streams = [t.section_id for t in templates if t.section_id.startswith("Streaming/channels/")]
    assert streams == [f"Streaming/channels/{n}" for n in ("101", "102", "201", "202")]


def test_expected_and_observed(camera):
    camera()
    result = read_all()
    assert COLLECTOR.expected("картинка") == ("Image/channels/*",)
    assert COLLECTOR.expected("события") == ()
    assert COLLECTOR.observed(result) == [
        "Image/channels/1",
        "System/Video/inputs/channels/1/overlays",
        "Streaming/channels/101",
    ]


def test_restore_gain_like_111_end_to_end(camera):
    api = camera()
    m0 = read_all()
    side_effect_like_111(api)
    onvif_set_imaging(api, "<tt:Brightness>80</tt:Brightness>")
    m1 = read_all()
    shifted = {c.id for c in diff(m0, m1)}
    assert shifted == {"Image/channels/1/Color/brightnessLevel", "Image/channels/1/Gain/GainLevel"}

    gain = m1.params()["Image/channels/1/Gain/GainLevel"]
    write = COLLECTOR.restore_request(gain, m0)
    assert (write.method, write.path) == ("PUT", "/ISAPI/Image/channels/1/gain")
    assert "<GainLevel>100</GainLevel>" in write.body
    response = http.request(
        "PUT",
        f"http://127.0.0.1:9100{write.path}",
        content=write.body,
        headers={"Content-Type": "application/xml"},
        login=HttpLogin("admin", SECRET),
    )
    assert response.status_code == 200
    assert read_all().params()["Image/channels/1/Gain/GainLevel"].value == "100"


def test_restore_only_for_listed_image_subresources(camera):
    camera()
    m0 = read_all()
    params = m0.params()
    for key in (
        "Streaming/channels/101/Video/constantBitRate",
        "Image/channels/1/ImageFlip/enabled",
        "System/Video/inputs/channels/1/overlays/TextOverlayList/TextOverlay[1]/enabled",
    ):
        assert COLLECTOR.restore_request(params[key], m0) is None, key
