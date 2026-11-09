"""Виртуальная камера с родным API (api-discovery, T4): вход Digest нашим клиентом, общее
состояние с ONVIF, режимы живых камер 09.11 и сырые ответы для проверок разбора."""

import xml.etree.ElementTree as ET

import httpx
import pytest

from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.http import HttpLogin, LoginRefused
from tests.mock_camera import MockCamera, Passport
from tests.mock_native import (
    HIK_NS,
    bitrate_edge_like_108,
    dahua,
    hikvision,
    side_effect_like_111,
    sticky_like_tiandy,
)
from tests.mock_onvif import OnvifServices

SECRET = "test-secret-2"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
BASE = "http://127.0.0.1:9100"


@pytest.fixture
def install(monkeypatch):
    def put(api):
        monkeypatch.setattr(http, "_TRANSPORT", api.transport())
        return api

    return put


def get(path: str, login: HttpLogin | None = None) -> httpx.Response:
    return http.request("GET", BASE + path, login=login or HttpLogin("admin", SECRET))


def leaf(response: httpx.Response, path: str) -> str:
    root = ET.fromstring(response.content)
    return root.findtext("/".join(f"h:{p}" for p in path.split("/")), namespaces={"h": HIK_NS})


def kv(response: httpx.Response) -> dict[str, str]:
    return dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)


def onvif_set_imaging(api, inner: str) -> None:
    """Запись картинки по ONVIF тем же обработчиком, что у виртуальной ONVIF-камеры."""
    services = OnvifServices(api.state, BASE, "rtsp://127.0.0.1/live")
    request = ET.fromstring(
        '<timg:SetImagingSettings xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><timg:VideoSourceToken>vs-1'
        f"</timg:VideoSourceToken><timg:ImagingSettings>{inner}</timg:ImagingSettings>"
        "</timg:SetImagingSettings>"
    )
    assert services.op_SetImagingSettings(request) is not None


def test_digest_login_with_our_client_and_counter(install):
    api = install(hikvision(password=SECRET))
    login = HttpLogin("admin", SECRET)
    first = get("/ISAPI/System/deviceInfo", login)
    second = get("/ISAPI/Image/channels/1", login)
    assert first.status_code == second.status_code == 200
    assert leaf(first, "model") == "DS-2CD2043G2-I"
    # первый запрос — без заголовка (401 с запросом входа), дальше nc растёт без лишних 401
    assert len(api.requests) == 3 and api.refusals == 0 and login.count == 2


def test_wrong_password_is_one_refusal_and_lock_after_n(install):
    api = install(hikvision(password=SECRET, lock_after=2))
    with pytest.raises(LoginRefused):
        get("/ISAPI/System/deviceInfo", HttpLogin("admin", "wrong-1"))
    assert api.refusals == 1 and not api.locked
    with pytest.raises(LoginRefused):
        get("/ISAPI/System/deviceInfo", HttpLogin("admin", "wrong-2"))
    assert api.locked
    # заблокированная камера не пускает и с верным паролем — поэтому вход один на прогон
    with pytest.raises(LoginRefused):
        get("/ISAPI/System/deviceInfo")


def test_stale_nonce_is_retried_not_refused(install):
    api = install(hikvision(password=SECRET, stale_once=True))
    assert get("/ISAPI/System/deviceInfo").status_code == 200
    assert api.refusals == 0


def test_onvif_write_is_visible_in_isapi(install):
    api = install(hikvision(password=SECRET))
    onvif_set_imaging(api, "<tt:Brightness>80</tt:Brightness>")
    image = get("/ISAPI/Image/channels/1")
    assert leaf(image, "Color/brightnessLevel") == "80"
    assert leaf(image, "Gain/GainLevel") == "100"
    assert leaf(image, "Exposure/ExposureType") == "auto"


def test_scale_and_capabilities(install):
    api = hikvision(
        password=SECRET, scale={"Image/channels/1/Color/brightnessLevel": (100 / 255, 0)}
    )
    install(api)
    onvif_set_imaging(api, "<tt:Brightness>204</tt:Brightness>")
    assert leaf(get("/ISAPI/Image/channels/1"), "Color/brightnessLevel") == "80"
    caps = ET.fromstring(get("/ISAPI/Image/channels/1/capabilities").content)
    level = caps.find("h:Color/h:brightnessLevel", {"h": HIK_NS})
    assert (level.get("min"), level.get("max")) == ("0", "100")
    exposure = caps.find("h:Exposure/h:ExposureType", {"h": HIK_NS})
    assert exposure.get("opt") == "auto,manual"


def test_side_effect_like_111(install):
    api = install(hikvision(password=SECRET))
    side_effect_like_111(api)
    onvif_set_imaging(api, "<tt:Brightness>80</tt:Brightness>")
    image = get("/ISAPI/Image/channels/1")
    assert leaf(image, "Color/brightnessLevel") == "80"
    assert leaf(image, "Gain/GainLevel") == "0"
    assert "<tt:MaxGain>0</tt:MaxGain>" in api.state.imaging_xml
    assert api.state.imaging_xml.count("MaxGain>") == 2  # MaxGain не задвоился


def test_native_put_restores_gain_into_onvif(install):
    api = install(hikvision(password=SECRET))
    side_effect_like_111(api)
    onvif_set_imaging(api, "<tt:Brightness>80</tt:Brightness>")
    body = f'<Gain xmlns="{HIK_NS}"><GainLevel>100</GainLevel></Gain>'
    put = http.request(
        "PUT",
        BASE + "/ISAPI/Image/channels/1/Gain",
        content=body,
        headers={"Content-Type": "application/xml"},
        login=HttpLogin("admin", SECRET),
    )
    assert put.status_code == 200
    assert "<tt:MaxGain>100</tt:MaxGain>" in api.state.imaging_xml


def test_pinned_mismatch_like_111(install):
    api = install(
        hikvision(password=SECRET, pinned={"Image/channels/1/Exposure/ExposureType": "manual"})
    )
    assert "<tt:Mode>AUTO</tt:Mode>" in api.state.imaging_xml
    assert leaf(get("/ISAPI/Image/channels/1"), "Exposure/ExposureType") == "manual"


def test_sticky_like_tiandy_keeps_old_value_on_both_sides(install):
    api = install(hikvision(password=SECRET))
    sticky_like_tiandy(api.state, "Brightness", "50")
    onvif_set_imaging(api, "<tt:Brightness>80</tt:Brightness>")
    assert leaf(get("/ISAPI/Image/channels/1"), "Color/brightnessLevel") == "50"


def test_status_is_volatile_and_sensitive_sections_exist(install):
    install(hikvision(password=SECRET))
    first = leaf(get("/ISAPI/System/status"), "CPUList/CPU/cpuUtilization")
    second = leaf(get("/ISAPI/System/status"), "CPUList/CPU/cpuUtilization")
    assert first != second
    assert leaf(get("/ISAPI/System/Network/interfaces/1"), "IPAddress/ipAddress") == "127.0.0.1"
    overlays = ET.fromstring(get("/ISAPI/System/Video/inputs/channels/1/overlays").content)
    assert len(overlays.findall(".//h:TextOverlay", {"h": HIK_NS})) == 2


def test_unknown_isapi_section_is_404(install):
    install(hikvision(password=SECRET))
    response = get("/ISAPI/System/Audio/channels/1/capabilities")
    assert response.status_code == 404 and b"notSupport" in response.content


def test_overrides_serve_raw_bombs_after_login(install):
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    api = install(hikvision(password=SECRET))
    api.overrides["/ISAPI/Image/channels/1/capabilities"] = (200, "application/xml", bomb)
    assert get("/ISAPI/Image/channels/1/capabilities").content == bomb


def test_dahua_cgi_copies_shift_together(install):
    api = install(dahua(password=SECRET))
    onvif_set_imaging(api, "<tt:Brightness>70</tt:Brightness>")
    color = kv(get("/cgi-bin/configManager.cgi?action=getConfig&name=VideoColor"))
    assert [color[f"table.VideoColor[0][{i}].Brightness"] for i in range(3)] == ["70"] * 3
    assert color["table.VideoColor[0][0].Hue"] == "50"


def test_dahua_device_and_caps(install):
    install(dahua(password=SECRET))
    assert (
        get("/cgi-bin/magicBox.cgi?action=getDeviceType").text.strip() == "type=IPC-HFW2431S-S-S2"
    )
    caps = kv(get("/cgi-bin/encode.cgi?action=getConfigCaps&channel=1"))
    assert caps["caps[0].MainFormat[0].Video.BitRateOptions[1]"] == "5632"
    assert caps["caps[0].MainFormat[0].Video.CompressionTypes"] == "H.264,H.265"
    bad = get("/cgi-bin/configManager.cgi?action=getConfig&name=NoSuchTable")
    assert bad.status_code == 400


def test_dahua_bitrate_edge_like_108(install):
    api = install(dahua(password=SECRET))
    bitrate_edge_like_108(api.state)
    services = OnvifServices(api.state, BASE, "rtsp://127.0.0.1/live")
    request = ET.fromstring(
        '<tr2:SetVideoEncoderConfiguration xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><tr2:Configuration token="ve-main">'
        "<tt:Encoding>H264</tt:Encoding><tt:RateControl><tt:BitrateLimit>5632</tt:BitrateLimit>"
        "</tt:RateControl></tr2:Configuration></tr2:SetVideoEncoderConfiguration>"
    )
    assert services.op_SetVideoEncoderConfiguration(request) is not None
    encode = kv(get("/cgi-bin/configManager.cgi?action=getConfig&name=Encode"))
    assert encode["table.Encode[0].MainFormat[0].Video.BitRate"] == "1792"


def test_dahua_set_config_writes_onvif(install):
    api = install(dahua(password=SECRET))
    response = get("/cgi-bin/configManager.cgi?action=setConfig&VideoInOptions[0].Gain=80")
    assert response.text.strip() == "OK"
    assert "<tt:MaxGain>80</tt:MaxGain>" in api.state.imaging_xml
    unknown = get("/cgi-bin/configManager.cgi?action=setConfig&VideoColor[0][0].Nope=1")
    assert unknown.status_code == 400
    # запись сети до камеры не доходит вовсе: её останавливает табу клиента
    with pytest.raises(TabooViolation):
        get("/cgi-bin/configManager.cgi?action=setConfig&Network.eth0.IPAddress=1.2.3.4")
    assert not any("Network" in r for r in api.requests)


def test_mock_camera_serves_native_on_onvif_port():
    api = hikvision(password=SECRET)
    camera = MockCamera(Passport("Hikvision", "DS-2CD2043G2-I", "V5.7.3", "SN1"), native=api)
    camera.start()
    try:
        url = f"http://127.0.0.1:{camera.port}/ISAPI/System/deviceInfo"
        response = http.request("GET", url, login=HttpLogin("admin", SECRET))
        assert response.status_code == 200 and leaf(response, "model") == "DS-2CD2043G2-I"
        assert camera.state is api.state
    finally:
        camera.stop()
