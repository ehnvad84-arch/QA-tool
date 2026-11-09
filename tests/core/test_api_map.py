"""Модель карты родного API: чувствительность, файлы версии 1, пределы, разность снимков (T3)."""

import json

import pytest

from qa_tool.core import api_map
from qa_tool.core.api_map import (
    MapError,
    MapFormatError,
    NativeMap,
    Pair,
    PairTable,
    Param,
    Scale,
    Section,
    Sensitivity,
    allowed_length,
    allowed_range,
    allowed_values,
    classify,
    diff,
    dumps_map,
    dumps_pairs,
    loads_map,
    loads_pairs,
    read_map,
    read_pairs,
    volatile_ids,
    write_map,
    write_pairs,
)

NORMAL, SENSITIVE, SECRET = Sensitivity.NORMAL, Sensitivity.SENSITIVE, Sensitivity.SECRET


@pytest.mark.parametrize(
    ("path", "level"),
    [
        ("Image/channels/1/Color/brightnessLevel", NORMAL),
        ("Image/channels/1/Gain/GainLevel", NORMAL),
        ("Image/channels/1/WhiteBalance/WhiteBalanceStyle", NORMAL),
        ("Streaming/channels/101/Video/keyFrameInterval", NORMAL),
        ("VideoColor[0][0].Brightness", NORMAL),
        ("Encode[0].MainFormat[0].Video.BitRate", NORMAL),
        ("/api/video/stream#/0/bitrate", NORMAL),
        ("System/Network/interfaces/1/IPAddress/ipAddress", SENSITIVE),
        ("Network.eth0.IPAddress", SENSITIVE),
        ("RTSP.Port", SENSITIVE),
        ("Streaming/channels/101/Multicast/destIPAddress", SENSITIVE),
        ("Streaming/channels/101/multicastAddress", SENSITIVE),
        ("Email.SmtpServer", SENSITIVE),
        ("Storage.FTP.Address", SENSITIVE),
        ("Security/users/1/userName", SENSITIVE),
        ("Security/users/1/password", SECRET),
        ("Network.PPPoE.Passwd", SECRET),
        ("Https.PrivateKey", SECRET),
        ("Cloud.private_key", SECRET),
        ("Platform.apiKey", SECRET),
        ("Session.Token", SECRET),
        ("Auth.Salt", SECRET),
        ("Wifi.PSK", SECRET),
        ("Https.Certificates", SECRET),
        ("Пользователь.Пароль", SECRET),
    ],
)
def test_classify_by_words(path, level):
    assert classify(path) is level


def test_section_class_and_inheritance_only_raise_level():
    assert classify("System/time/localTime", "сеть и безопасность") is SENSITIVE
    assert classify("Color/brightnessLevel", "картинка", SENSITIVE) is SENSITIVE
    assert classify("Color/brightnessLevel", "картинка", SECRET) is SECRET
    # имя раздела «Network» делает раздел чувствительным, даже если сборщик дал «прочее»
    assert Section("System/Network/interfaces", "прочее", "GET …").sensitivity is SENSITIVE


def test_add_param_drops_values_of_secrets_and_sensitive():
    image = Section("Image/channels/1", "картинка", "GET /ISAPI/Image/channels/1")
    kept = image.add_param("Image/channels/1/Color/brightnessLevel", "целое", value=50)
    secret = image.add_param("Image/channels/1/Upload/token", "строка", value="abc123")
    assert kept.value == "50" and kept.sensitivity is NORMAL
    assert secret.value is None and secret.sensitivity is SECRET

    net = Section("System/time", "сеть и безопасность", "GET /ISAPI/System/time")
    param = net.add_param("System/time/timeZone", "строка", value="CST-3")
    assert param.value is None and param.sensitivity is SENSITIVE


def test_add_param_rejects_bad_ids_and_clips_values():
    section = Section("Image", "картинка", "GET /x")
    assert section.add_param("") is None
    assert section.add_param("x" * (api_map.MAX_ID_LEN + 1)) is None
    long = section.add_param("Overlay/text", "строка", value="я" * 5000)
    assert len(long.value) == api_map.MAX_VALUE_LEN and long.value.endswith(api_map.CLIPPED)
    assert section.add_param("Overlay", "объект", value="<x/>").value is None
    assert section.add_param("Overlay/kind", "словарь").type == "неизвестно"


@pytest.mark.parametrize(
    ("lo", "hi", "expected"),
    [
        (0, 100, {"мин": 0, "макс": 100}),
        ("0", "255", {"мин": 0, "макс": 255}),
        (0.5, 1.5, {"мин": 0.5, "макс": 1.5}),
    ],
)
def test_allowed_range_good(lo, hi, expected):
    assert allowed_range(lo, hi) == expected


@pytest.mark.parametrize(
    ("lo", "hi"),
    [
        (100, 0),
        ("nan", 10),
        (0, float("inf")),
        (0, "1e999"),
        (0, 2e9),
        (None, 10),
        (True, 10),
        ("1" * 5000, 10),
    ],
)
def test_allowed_range_garbage_is_marked(lo, hi):
    assert set(allowed_range(lo, hi)) == {"негодный"}


def test_allowed_step_values_and_length():
    assert allowed_range(512, 5632, 32) == {"мин": 512, "макс": 5632, "шаг": 32}
    assert allowed_range(0, 10, 0) == {"мин": 0, "макс": 10}
    assert allowed_range(0, 10, "nan") == {"мин": 0, "макс": 10}
    assert allowed_values(["auto", "manual"]) == {"значения": ["auto", "manual"]}
    assert set(allowed_values(list(range(257)))) == {"негодный"}
    assert allowed_length("32") == {"длина_макс": 32}
    assert allowed_length(-1) is None
    assert allowed_length("1.5") is None


def _sample_map() -> NativeMap:
    native = NativeMap(
        brand="hikvision",
        collector="ISAPI",
        maker="Hikvision",
        model="DS-2CD2043G2-I",
        firmware="V5.7.3",
        run="QA-20261109-003",
        place=2,
        time="2026-11-09T10:12:00+00:00",
        login="Digest MD5",
    )
    image = Section(
        "Image/channels/1",
        "картинка",
        "GET /ISAPI/Image/channels/1",
        describe="GET /ISAPI/Image/channels/1/capabilities",
        code=200,
        size=3512,
    )
    image.add_param("Image/channels/1/Color/brightnessLevel", "целое", allowed_range(0, 100), "50")
    image.add_param(
        "Image/channels/1/Exposure/ExposureType",
        "перечисление",
        allowed_values(["auto", "manual"]),
        "manual",
    )
    image.add_param("Image/channels/1/Gain/GainLevel", "целое", allowed_range(0, 100), "100")
    image.add_param("Image/channels/1/Status/temperature", "число", None, "41.5", volatile=True)
    image.add_param("Image/channels/1/id", "целое", None, "1", writable=False)
    net = Section("System/Network/interfaces/1", "сеть и безопасность", "GET …", code=200)
    net.add_param("System/Network/interfaces/1/MTU", "целое", allowed_range(1280, 1500), "1500")
    native.sections += [image, net]
    native.errors.append(
        MapError("GET /ISAPI/System/Audio/channels/1/capabilities", 404, "раздела нет у модели")
    )
    return native


def test_map_round_trip(tmp_path):
    native = _sample_map()
    path = tmp_path / api_map.MAP_FILE
    write_map(path, native)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["формат"] == "qa-tool/api-map" and data["версия"] == 1
    assert data["камера"]["модель"] == "DS-2CD2043G2-I"
    assert data["разделы"][1]["параметры"][0]["значение"] is None  # MTU — сеть

    back = read_map(path)
    assert back == native
    assert back.param_count() == 6
    assert back.params()["Image/channels/1/id"].writable is False


def test_writer_never_writes_sensitive_values_even_if_put_directly():
    native = _sample_map()
    native.sections[0].params.append(
        Param("Image/channels/1/password", "строка", value="12345", sensitivity=SECRET)
    )
    assert "12345" not in dumps_map(native)


def test_loader_recomputes_sensitivity_of_tampered_file():
    data = json.loads(dumps_map(_sample_map()))
    data["разделы"][0]["параметры"].append(
        {
            "id": "Image/channels/1/adminPassword",
            "тип": "строка",
            "допустимые": None,
            "значение": "12345",
            "чувствительность": "обычный",
            "изменчивый": False,
        }
    )
    data["разделы"][1]["чувствительность"] = "обычный"
    data["разделы"][1]["параметры"][0]["значение"] = "1500"
    back = loads_map(json.dumps(data, ensure_ascii=False))
    password = back.params()["Image/channels/1/adminPassword"]
    assert password.value is None and password.sensitivity is SECRET
    assert back.params()["System/Network/interfaces/1/MTU"].value is None


def test_loader_keeps_stricter_stored_level():
    data = json.loads(dumps_map(_sample_map()))
    data["разделы"][0]["параметры"][0]["чувствительность"] = "чувствительный"
    param = loads_map(json.dumps(data, ensure_ascii=False)).params()[
        "Image/channels/1/Color/brightnessLevel"
    ]
    assert param.sensitivity is SENSITIVE and param.value is None


def _tamper(change) -> str:
    data = json.loads(dumps_map(_sample_map()))
    change(data)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "не json",
        '{"формат": NaN}',
        "[" * 100_000 + "]" * 100_000,
        '{"a": ' + "1" * 5000 + "}",
        _tamper(lambda d: d.update({"формат": "чужой"})),
        _tamper(lambda d: d.update({"версия": 2})),
        _tamper(lambda d: d.update({"лишний": 1})),
        _tamper(lambda d: d["разделы"][0]["параметры"][0].update({"id": "x" * 300})),
        _tamper(lambda d: d["разделы"][0]["параметры"][0]["допустимые"].update({"макс": 1e300})),
        _tamper(lambda d: d["разделы"][0].update({"класс": "секретный"})),
        _tamper(lambda d: d["разделы"][0]["параметры"][0].update({"значение": 50})),
    ],
)
def test_loader_rejects_hostile_or_foreign_files(text):
    with pytest.raises(MapFormatError):
        loads_map(text)


def test_loader_rejects_infinity_written_as_big_exponent():
    text = dumps_map(_sample_map()).replace('"макс": 100', '"макс": 1e999', 1)
    assert "1e999" in text
    with pytest.raises(MapFormatError):
        loads_map(text)


def test_read_limits_size_and_encoding(tmp_path, monkeypatch):
    big = tmp_path / "big.json"
    big.write_bytes(b" " * 100)
    monkeypatch.setattr(api_map, "MAX_MAP_BYTES", 50)
    with pytest.raises(MapFormatError, match="больше"):
        read_map(big)
    broken = tmp_path / "broken.json"
    broken.write_bytes(b"\xff\xfe")
    with pytest.raises(MapFormatError, match="UTF-8"):
        read_map(broken)
    with pytest.raises(MapFormatError, match="не прочитан"):
        read_map(tmp_path / "нет.json")


def test_too_many_params_are_cut_with_reason():
    native = NativeMap(brand="dahua", collector="CGI")
    for s in range(3):
        section = Section(f"Encode{s}", "кодировщик", "GET /cgi-bin/configManager.cgi")
        for i in range(2000):
            section.add_param(f"Encode[{s}].Param{i}", "целое", None, str(i))
        native.sections.append(section)
    back = loads_map(dumps_map(native))
    assert back.param_count() == api_map.MAX_PARAMS
    assert back.cut and "записаны первые 5000" in back.cut_reason


def test_oversized_file_drops_sections_from_the_end(monkeypatch):
    native = _sample_map()
    full = len(dumps_map(native).encode("utf-8"))
    monkeypatch.setattr(api_map, "MAX_MAP_BYTES", full - 1)
    back = loads_map(dumps_map(native))
    assert [s.id for s in back.sections] == ["Image/channels/1"]
    assert back.cut and "записаны первые 1 разделов" in back.cut_reason


def test_invalid_map_is_not_written(tmp_path):
    native = _sample_map()
    native.sections[0].params.append(Param("x", "целое", allowed={"мин": 0}, value="1"))
    with pytest.raises(MapFormatError):
        write_map(tmp_path / api_map.MAP_FILE, native)
    assert not (tmp_path / api_map.MAP_FILE).exists()


def _snapshot(values: dict[str, str], volatile: frozenset[str] = frozenset()) -> NativeMap:
    native = NativeMap(brand="hikvision", collector="ISAPI")
    section = Section("Image/channels/1", "картинка", "GET /ISAPI/Image/channels/1")
    for key, value in values.items():
        section.add_param(key, "строка", None, value, volatile=key in volatile)
    native.sections.append(section)
    return native


def test_diff_finds_side_effect_like_camera_111():
    # 09.11, Hikvision .111: запись яркости по ONVIF сдвинула и яркость, и усиление
    m0 = _snapshot({"Color/brightnessLevel": "50", "Gain/GainLevel": "100", "Sharp": "50"})
    m1 = _snapshot({"Color/brightnessLevel": "80", "Gain/GainLevel": "0", "Sharp": "50"})
    assert diff(m0, m1) == [
        api_map.Change("Color/brightnessLevel", "50", "80"),
        api_map.Change("Gain/GainLevel", "100", "0"),
    ]


def test_diff_compares_canonical_values():
    m0 = _snapshot({"a": "50", "b": " Auto ", "c": "true", "d": "nan"})
    m1 = _snapshot({"a": "50.0", "b": "auto", "c": "TRUE", "d": "NaN"})
    assert diff(m0, m1) == []
    assert diff(_snapshot({"a": "1"}), _snapshot({"a": "один"})) != []


def test_diff_ignores_volatile_sensitive_and_appearing_params():
    m0 = _snapshot({"temp": "41", "Codec/H264/profile": "main", "Upload/token": "a"})
    m1 = _snapshot({"temp": "43", "Codec/H265/tier": "main", "Upload/token": "b"})
    assert diff(m0, m1, frozenset({"temp"})) == []

    flagged = _snapshot({"temp": "41"}, volatile=frozenset({"temp"}))
    assert diff(flagged, _snapshot({"temp": "43"})) == []


def test_volatile_ids_from_two_baselines_and_marks():
    m0a = _snapshot({"clock": "10:00:01", "level": "50", "fps": "25"}, frozenset({"fps"}))
    m0b = _snapshot({"clock": "10:00:03", "level": "50", "fps": "25"})
    assert volatile_ids(m0a, m0b) == {"clock", "fps"}


def _sample_pairs() -> PairTable:
    return PairTable(
        "Hikvision",
        "DS-2CD2043G2-I",
        "V5.7.3",
        [
            Pair(
                1,
                "Imaging/Brightness",
                "Image/channels/1/Color/brightnessLevel",
                Scale("линейная", a=0.392, b=0.0),
                points=((128.0, 50.0), (204.0, 80.0), (51.0, 20.0)),
                source="найдено автоматически, прогон QA-20261109-003",
            ),
            Pair(
                12,
                "Exposure/Mode",
                "Image/channels/1/Exposure/ExposureType",
                Scale("перечисление", values={"AUTO": "auto", "MANUAL": "manual"}),
                state="подтверждена вручную",
                source="подтверждено вручную: директор, 09.11, .114",
            ),
            Pair(
                13,
                "Exposure/MaxGain",
                "Image/channels/1/Gain/GainLevel",
                Scale("по диапазонам"),
                state="подтверждена вручную",
                source="подтверждено вручную: директор, 09.11, .112 и .114",
                models=("DS-2CD2043G2-I",),
            ),
        ],
    )


def test_pairs_round_trip(tmp_path):
    table = _sample_pairs()
    path = tmp_path / api_map.PAIRS_FILE
    write_pairs(path, table)
    assert read_pairs(path) == table
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "модели" not in data["пары"][0] and data["пары"][2]["модели"] == ["DS-2CD2043G2-I"]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["пары"][0]["шкала"].pop("a"),
        lambda d: d["пары"][1]["шкала"].pop("значения"),
        lambda d: d["пары"][0]["шкала"].update({"вид": "квадратичная"}),
        lambda d: d["пары"][0].update({"состояние": "угадана"}),
        lambda d: d["пары"][0].update({"n": 0}),
        lambda d: d["пары"][0].update({"точки": [[1, 2, 3]]}),
        lambda d: d["пары"][0].update({"родной": ""}),
        lambda d: d.update({"версия": 2}),
    ],
)
def test_pairs_loader_rejects_broken_tables(change):
    data = json.loads(dumps_pairs(_sample_pairs()))
    change(data)
    with pytest.raises(MapFormatError):
        loads_pairs(json.dumps(data, ensure_ascii=False))


def test_pairs_writer_refuses_infinite_scale():
    table = _sample_pairs()
    table.pairs[0] = Pair(1, "Imaging/Brightness", "x", Scale("линейная", a=float("inf"), b=0))
    with pytest.raises((MapFormatError, ValueError)):
        dumps_pairs(table)
