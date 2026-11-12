"""Сборщик Dahua CGI (api-discovery, T7): таблицы настроек, описание кодировщика из
`getConfigCaps`, копии по профилям, ответ «Error», возврат одним ключом `setConfig`."""

import pytest

from qa_tool.core.api_map import NativeMap, diff
from qa_tool.core.models import Camera
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.dahua.collector import DahuaCollector
from qa_tool.drivers.camera.http import HttpLogin
from qa_tool.drivers.camera.native import NativeReader, ParseRefused, ReadTemplate
from tests.mock_native import bitrate_edge_like_108, dahua, onvif_set_imaging

SECRET = "test-secret-5"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
CAMERA = Camera(slot=2, address="127.0.0.1", username="admin", port=9108, vendor="dahua")
COLLECTOR = DahuaCollector()
ENCODE = ReadTemplate(
    "/cgi-bin/configManager.cgi",
    "Encode",
    "кодировщик",
    query="action=getConfig&name=Encode",
    describe_path="/cgi-bin/encode.cgi?action=getConfigCaps&channel=1",
)

# Сокращённые ответы по образцу .108 09.11: идентификаторов камеры в них нет.
ENCODE_108 = (
    b"table.Encode[0].MainFormat[0].Video.BitRate=2304\r\n"
    b"table.Encode[0].MainFormat[0].Video.BitRateControl=CBR\r\n"
    b"table.Encode[0].MainFormat[0].Video.Compression=H.265\r\n"
    b"table.Encode[0].MainFormat[0].Video.FPS=25.000000\r\n"
    b"table.Encode[0].ExtraFormat[0].Video.BitRate=512\r\n"
    b"table.VideoColor[0][0].Brightness=50\r\n"
)
CAPS_108 = (
    b"caps[0].BitRateRange[0]=3\r\n"
    b"caps[0].MainFormat[0].Video.BitRateOptions=512,5632\r\n"
    b"caps[0].MainFormat[0].Video.CompressionTypes=H.264,H.265\r\n"
    b"caps[0].ExtraFormat[0].Video.BitRateOptions=188,1536\r\n"
    b"caps[0].MainFormat[0].Video.FPSMax=25\r\n"
)


@pytest.fixture
def camera(monkeypatch):
    def put(**kwargs):
        api = dahua(password=SECRET, **kwargs)
        monkeypatch.setattr(http, "_TRANSPORT", api.transport())
        return api

    return put


def read_all() -> NativeMap:
    meta = NativeMap(brand="dahua", collector="CGI")
    return NativeReader(CAMERA, COLLECTOR, HttpLogin("admin", SECRET)).read(meta)


def test_map_of_virtual_dahua(camera):
    api = camera()
    result = read_all()
    assert [s.id for s in result.sections] == [
        "magicBox.getDeviceType",
        "magicBox.getSoftwareVersion",
        "VideoColor",
        "VideoInOptions",
        "Encode",
        "VideoWidget",
    ]
    params = result.params()
    assert params["magicBox.getDeviceType.type"].value == "IPC-HFW2431S-S-S2"
    assert params["VideoColor[0][2].Brightness"].type == "целое"
    bitrate = params["Encode[0].MainFormat[0].Video.BitRate"]
    assert bitrate.allowed == {"мин": 512, "макс": 5632} and bitrate.value == "4000"
    assert params["Encode[0].MainFormat[0].Video.Compression"].allowed == {
        "значения": ["H.264", "H.265"]
    }
    assert params["VideoWidget[0].ChannelTitle.EncodeBlend"].type == "логический"
    assert [(e.code, e.request) for e in result.errors] == [
        (400, "GET /cgi-bin/devVideoInput.cgi?action=getCaps&channel=1")
    ]
    assert not any("setConfig" in r or "Network" in r for r in api.requests)


def test_caps_of_108_give_ranges_and_enums():
    section = COLLECTOR.parse(ENCODE, ENCODE_108, "text/plain", CAPS_108)
    found = {p.id: p for p in section.params}
    assert found["Encode[0].MainFormat[0].Video.BitRate"].allowed == {"мин": 512, "макс": 5632}
    assert found["Encode[0].ExtraFormat[0].Video.BitRate"].allowed == {"мин": 188, "макс": 1536}
    assert found["Encode[0].MainFormat[0].Video.Compression"].type == "перечисление"
    assert found["Encode[0].MainFormat[0].Video.FPS"].type == "число"
    assert found["Encode[0].MainFormat[0].Video.FPS"].allowed is None
    # строка чужой таблицы в ответе Encode в раздел не попадает
    assert "VideoColor[0][0].Brightness" not in found


def test_garbage_caps_range_is_marked():
    caps = b"caps[0].MainFormat[0].Video.BitRateOptions=5632,512\r\n"
    section = COLLECTOR.parse(ENCODE, ENCODE_108, "text/plain", caps)
    bitrate = next(p for p in section.params if p.id.endswith("MainFormat[0].Video.BitRate"))
    assert set(bitrate.allowed) == {"негодный"}


@pytest.mark.parametrize("body", [b"Error\r\nBad Request!\r\n", b"<html>login</html>", b""])
def test_error_or_empty_reply_is_refused(body):
    with pytest.raises(ParseRefused):
        COLLECTOR.parse(ENCODE, body, "text/plain", None)


def test_bitrate_edge_like_108_is_declared_in_cgi(camera):
    api = camera()
    bitrate_edge_like_108(api.state)
    bitrate = read_all().params()["Encode[0].MainFormat[0].Video.BitRate"]
    assert bitrate.allowed == {"мин": 512, "макс": 5632}


def test_copies_by_profile_shift_together(camera):
    api = camera()
    m0 = read_all()
    onvif_set_imaging(api, "<tt:Brightness>70</tt:Brightness>")
    shifted = {c.id for c in diff(m0, read_all())}
    assert shifted == {f"VideoColor[0][{i}].Brightness" for i in range(3)}


def test_restore_one_key_with_m0_value(camera):
    api = camera()
    m0 = read_all()
    api.extras["VideoColor[0][*].Gamma"] = "80"
    m1 = read_all()
    gamma = m1.params()["VideoColor[0][1].Gamma"]
    assert gamma.value == "80"
    write = COLLECTOR.restore_request(gamma, m0)
    assert (write.method, write.path, write.query) == (
        "GET",
        "/cgi-bin/configManager.cgi",
        "action=setConfig&VideoColor[0][1].Gamma=50",
    )
    response = http.request(
        "GET", f"http://127.0.0.1:9108{write.path}?{write.query}", login=HttpLogin("admin", SECRET)
    )
    assert response.text.strip() == "OK"
    assert read_all().params()["VideoColor[0][1].Gamma"].value == "50"


def test_restore_only_image_tables(camera):
    camera()
    m0 = read_all()
    params = m0.params()
    for key in ("Encode[0].MainFormat[0].Video.BitRate", "VideoWidget[0].ChannelTitle.Name"):
        assert COLLECTOR.restore_request(params[key], m0) is None, key


def test_restore_value_is_url_encoded_and_limited():
    m0 = NativeMap(brand="dahua", collector="CGI")
    section = COLLECTOR.parse(
        ReadTemplate("/cgi-bin/configManager.cgi", "VideoInOptions", "картинка"),
        b"table.VideoInOptions[0].Mode=a&b=c\r\ntable.VideoInOptions[0].Long=" + b"x" * 300,
        "text/plain",
        None,
    )
    m0.sections.append(section)
    params = m0.params()
    write = COLLECTOR.restore_request(params["VideoInOptions[0].Mode"], m0)
    assert write.query == "action=setConfig&VideoInOptions[0].Mode=a%26b%3Dc"
    assert COLLECTOR.restore_request(params["VideoInOptions[0].Long"], m0) is None


def test_expected_and_observed(camera):
    camera()
    result = read_all()
    assert COLLECTOR.expected("картинка") == ("VideoColor", "VideoInOptions")
    assert COLLECTOR.expected("кодировщик") == ("Encode",)
    assert COLLECTOR.observed(result) == ["VideoColor", "VideoInOptions", "Encode", "VideoWidget"]
