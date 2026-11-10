"""Карта по трафику веб-обхода (api-discovery, T10, S2): разбор без своих запросов к камере."""

from qa_tool.core.api_map import NativeMap, Sensitivity, dumps_map
from qa_tool.drivers.camera.browser import StoppedRequest, TrafficEntry, Verdict
from qa_tool.drivers.camera.traffic import SOURCE, STOPPED, from_traffic

BASE = "http://10.0.0.5"


def get(path, body, kind="application/json", status=200):
    return TrafficEntry("GET", BASE + path, status, kind, body.encode())


def build(entries, stopped=()):
    return from_traffic(entries, stopped, NativeMap(brand="tiandy", collector="трафик"))


def test_json_xml_and_key_value_become_params_with_values():
    native = build(
        [
            get("/api/image", '{"brightness": 50, "wdr": {"on": true, "level": 2.5}}'),
            get("/cgi/osd.xml", "<Osd><Title>cam</Title><Line/><Line/></Osd>", "application/xml"),
            get(
                "/cgi-bin/config?action=get&name=VideoColor",
                "table.VideoColor[0].Hue=50",
                "text/plain",
            ),
        ]
    )
    params = native.params()
    assert native.source == SOURCE
    assert params["/api/image#/brightness"].value == "50"
    assert params["/api/image#/brightness"].type == "целое"
    assert params["/api/image#/wdr/on"].type == "логический"
    assert params["/api/image#/wdr/level"].type == "число"
    assert params["/cgi/osd.xml#/Title"].value == "cam"
    assert "/cgi/osd.xml#/Line[2]" in params
    assert params["/cgi-bin/config?action=get&name=VideoColor#VideoColor[0].Hue"].value == "50"
    # класс по адресу: картинка для S5 и приложения
    image = next(s for s in native.sections if s.id == "/api/image")
    assert image.cls == "картинка"
    assert next(s for s in native.sections if s.id == "/cgi/osd.xml").cls == "наложения"
    dumps_map(native)  # карта проходит схему api-map.json


def test_write_keeps_field_names_only():
    write = TrafficEntry("PUT", BASE + "/api/image", 200, "", b"", ("brightness", "mode"))
    native = build([write])
    params = native.params()
    assert set(params) == {"PUT /api/image#/brightness", "PUT /api/image#/mode"}
    assert all(p.value is None for p in params.values())


def test_network_and_secrets_lose_values():
    native = build(
        [
            get("/api/network/interfaces", '{"ip": "10.0.0.5", "gateway": "10.0.0.1"}'),
            get("/api/image", '{"brightness": 50, "password": "x1"}'),
        ]
    )
    params = native.params()
    net = params["/api/network/interfaces#/ip"]
    assert net.value is None and net.sensitivity is not Sensitivity.NORMAL
    assert params["/api/image#/password"].value is None
    assert "10.0.0.5" not in dumps_map(native) and "x1" not in dumps_map(native)


def test_token_in_query_is_hidden_from_section_id():
    native = build([get("/api/image?session=0123456789abcdef0123&ch=1", '{"a": 1}')])
    assert [s.id for s in native.sections] == ["/api/image?session=…&ch=1"]


def test_errors_stopped_and_hostile_bodies_go_to_errors():
    stopped = [StoppedRequest("POST", BASE + "/api/users/1", Verdict.STOP_WRITE)]
    deep = "[" * 100 + "]" * 100
    native = build(
        [
            get("/api/missing", "{}", status=404),
            get("/api/dtd", '<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', "application/xml"),
            get("/api/nan", '{"a": NaN}'),
            get("/api/deep", deep),
        ],
        stopped,
    )
    reasons = {e.request: (e.code, e.reason) for e in native.errors}
    assert reasons["/api/missing"][0] == 404
    assert reasons["POST /api/users/1"] == (None, STOPPED)
    for bad in ("/api/dtd", "/api/nan", "/api/deep"):
        assert reasons[bad][1].startswith("не разобран"), bad
    assert native.sections == []


def test_repeated_request_counts_once_and_pages_without_data_are_skipped():
    native = build(
        [
            TrafficEntry("GET", BASE + "/", 200, "text/html", b""),
            get("/api/image", '{"brightness": 50}'),
            get("/api/image", '{"brightness": 60}'),
        ]
    )
    assert [s.id for s in native.sections] == ["/api/image"]
    assert native.params()["/api/image#/brightness"].value == "50"
