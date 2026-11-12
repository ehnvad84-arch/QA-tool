"""Автопоиск соответствий (api-discovery, T15): пары находятся записью по ONVIF, режимы попутно,
копии Dahua — одна пара, побочный эффект .111 останавливает поиск, всё возвращается."""

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api.discovery import CHECK_ID
from qa_tool.checks.api.search import SearchRefused, _shares, leaves_of
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.api_map import LIBRARY_KEY
from qa_tool.core.checks import REGISTRY, CheckContext, Severity, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.core.write_journal import API, load
from tests.mock_camera import MockCamera, Passport
from tests.mock_native import (
    bitrate_edge_like_108,
    dahua,
    hikvision,
    side_effect_like_111,
)

load_all()
SPEC = REGISTRY.get(CHECK_ID)
SECRET = "test-secret-11"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
FAST = {
    "api.apply_delay_s": Threshold(0.01, "с", "тест"),
    "api.baseline_gap_s": Threshold(0.01, "с", "тест"),
}


@pytest.fixture
def run(tmp_path):
    started = []

    def go(api, maker="Hikvision", model="DS-2CD2043G2-I", camera=None):
        if camera is None:
            camera = MockCamera(Passport(maker, model, "V1", "SN1"), password=SECRET, native=api)
            started.append(camera.start())
        place = Camera(slot=1, address="127.0.0.1", port=camera.port, username="admin")
        extra = {
            PASSPORT_KEY: {
                "manufacturer": maker,
                "model": model,
                "firmware": "V1",
                "serial": "SN1",
            },
            LIBRARY_KEY: str(tmp_path / "lib"),
            API.folder_key: str(tmp_path / "pending-api"),
            "run_id": "QA-T15",
        }
        ctx = CheckContext(
            place, SECRET, Place.COLOR_BOX, thresholds=FAST, extra=extra, allow_writes=True
        )
        return run_check(SPEC, ctx), camera

    yield go
    for camera in started:
        camera.stop()


def rows(result):
    return {c.check_id: c for c in result.children}


def pairs_of(result):
    trace = next(e.content for e in result.evidence if e.name == "api-pairs.json")
    import json

    return {p["n"]: p for p in json.loads(trace)["пары"]}


def test_hikvision_pairs_found_and_everything_returned(run, tmp_path):
    api = hikvision(password=SECRET)
    before = api.state.imaging_xml
    result, _ = run(api)
    found = rows(result)
    assert found["API-SEARCH-1"].result.status is Status.PASSED, found["API-SEARCH-1"].result
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    pairs = pairs_of(result)
    assert pairs[1]["родной"] == "Image/channels/1/Color/brightnessLevel"
    assert pairs[1]["шкала"]["вид"] == "линейная" and abs(pairs[1]["шкала"]["a"] - 1) < 1e-6
    assert pairs[6]["родной"] == "Image/channels/1/WhiteBalance/WhiteBalanceRed"
    assert pairs[5]["шкала"]["значения"] == {"AUTO": "auto", "MANUAL": "manual"}
    assert pairs[15]["родной"] == "Streaming/channels/101/Video/constantBitRate"
    assert pairs[16]["шкала"]["вид"] == "перечисление"
    # найденные пары сразу идут в сверку этого прогона
    assert found["API-MATCH-1"].result.status is Status.PASSED
    assert api.state.imaging_xml.count("<tt:Brightness>50</tt:Brightness>") == 1
    assert "<tt:MaxGain>100</tt:MaxGain>" in api.state.imaging_xml and before
    assert load(tmp_path / "pending-api", "127.0.0.1", _.port) is None  # журнал закрыт


def test_dahua_copies_are_one_pair(run):
    result, _ = run(dahua(password=SECRET), maker="Dahua", model="IPC-HFW2431S-S-S2")
    pairs = pairs_of(result)
    assert pairs[1]["родной"] == "VideoColor[0][0].Brightness"
    assert rows(result)["API-SEARCH-1"].result.status is Status.PASSED


def test_side_effect_like_111_stops_search_and_returns(run):
    api = hikvision(password=SECRET)
    side_effect_like_111(api)
    result, _ = run(api)
    found = rows(result)
    search = found["API-SEARCH-1"]
    assert search.result.status is Status.FAILED and search.severity is Severity.MINOR
    assert "Gain/GainLevel 100 → 0" in search.result.actual
    assert "после возврата всё вернулось" in search.result.actual
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    assert "<tt:MaxGain>100</tt:MaxGain>" in api.state.imaging_xml


def test_side_effect_that_onvif_cannot_return_is_returned_by_native_api(run):
    api = hikvision(password=SECRET)
    side_effect_like_111(api)
    # как на .111: MaxGain по ONVIF не возвращается, вернул PUT блока Gain в ISAPI
    api.state.imaging_sticky["MaxGain"] = "0"
    result, _ = run(api)
    found = rows(result)
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    assert "после возврата всё вернулось" in found["API-SEARCH-1"].result.actual
    assert api.requests.count("PUT /ISAPI/Image/channels/1/gain") == 1  # одна попытка
    assert "<tt:MaxGain>100</tt:MaxGain>" in api.state.imaging_xml


def test_native_return_refused_is_blocker_with_how_to_return(run, tmp_path):
    api = hikvision(password=SECRET)
    side_effect_like_111(api)
    api.state.imaging_sticky["MaxGain"] = "0"
    api.overrides["/ISAPI/Image/channels/1/gain"] = (403, "application/xml", b"<x/>")
    result, camera = run(api)
    restore = rows(result)["API-RESTORE-1"]
    assert restore.result.status is Status.FAILED and restore.severity is Severity.BLOCKER
    actual = restore.result.actual
    assert "Image/channels/1/Gain/GainLevel: было 100, осталось 0" in actual
    assert "PUT /ISAPI/Image/channels/1/gain" in actual and "пробовала один раз" in actual
    assert api.requests.count("PUT /ISAPI/Image/channels/1/gain") == 1  # без повтора
    # журнал остаётся до следующего прогона, в нём — что пробовали родным API
    entry = load(tmp_path / "pending-api", "127.0.0.1", camera.port)
    assert entry is not None and "Gain/GainLevel" in entry.restore["native"]


def test_old_journal_native_params_are_manual_blocker(run, tmp_path):
    from qa_tool.core.write_journal import PendingWrite, save

    api = hikvision(password=SECRET)
    # первый прогон поднимает камеру; журнал «прошлого» прогона подкладываем под её порт
    _, camera = run(api)
    native = '[{"id": "Image/channels/1/Gain/GainLevel", "запрос": "PUT /ISAPI/Image/channels/1/gain", "было": "100", "осталось": "0"}]'  # noqa: E501
    entry = PendingWrite(
        "OLD-RUN",
        1,
        "127.0.0.1",
        camera.port,
        "",
        "imaging",
        "Яркость",
        "1",
        "50",
        "50",
        "restoring",
        restore={"xml": "", "service": "imaging", "native": native},
    )
    save(tmp_path / "pending-api", entry)
    result, _ = run(api, camera=camera)
    lost = [c for c in result.children if c.check_id == "API-WRITE-LOST"]
    assert any(
        c.severity is Severity.BLOCKER and "Gain/GainLevel: было 100, осталось 0" in c.result.actual
        for c in lost
    )
    assert rows(result)["API-SEARCH-1"].result.status is Status.SKIPPED


def test_onvif_fault_on_one_setting_is_not_found_and_search_goes_on(run, tmp_path, monkeypatch):
    # B1 ревью api-discovery: Fault на одной записи раньше уничтожал всю проверку API-MAP-1
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import OnvifFault

    original = imaging.ImagingWriter.write

    def write(self, key, value, persist=False):
        if key == "sharpness":
            raise imaging.ValueRejected(OnvifFault("ter:InvalidArgVal"))
        return original(self, key, value, persist)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    api = hikvision(password=SECRET)
    result, camera = run(api)
    found = rows(result)
    assert {"API-MAP-1", "API-SEARCH-1", "API-RESTORE-1", "API-MATCH-1"} <= set(found)
    assert found["API-SEARCH-1"].result.status is Status.PASSED
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    pairs = pairs_of(result)
    assert 1 in pairs and 15 in pairs  # яркость до резкости и битрейт после неё не потеряны
    trace = next(e.content for e in result.evidence if e.name == "search.json")
    assert "не найдена: камера не приняла значение по ONVIF" in trace
    assert load(tmp_path / "pending-api", "127.0.0.1", camera.port) is None
    assert api.state.imaging_xml.count("<tt:Brightness>50</tt:Brightness>") == 1


def test_onvif_fault_on_read_stops_search_but_keeps_map(run, tmp_path, monkeypatch):
    # ревью, круг 3, W4: разовый Fault на чтении после записи уносил всю API-MAP-1
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import OnvifFault

    original_write, original_read = imaging.ImagingWriter.write, imaging.ImagingWriter.read
    armed = {"fault": False}

    def write(self, key, value, persist=False):
        if key == "sharpness":
            armed["fault"] = True
        return original_write(self, key, value, persist)

    def read(self):
        if armed["fault"]:
            armed["fault"] = False
            raise OnvifFault("ter:Action")
        return original_read(self)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    monkeypatch.setattr(imaging.ImagingWriter, "read", read)
    api = hikvision(password=SECRET)
    result, camera = run(api)
    found = rows(result)
    assert {"API-MAP-1", "API-SEARCH-1", "API-RESTORE-1", "API-MATCH-1"} <= set(found)
    search = found["API-SEARCH-1"].result
    assert search.status is Status.STAND_ERROR and "сбой ONVIF на чтении" in search.reason
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    assert 1 in pairs_of(result)  # пара, найденная до сбоя, не потеряна
    assert load(tmp_path / "pending-api", "127.0.0.1", camera.port) is None


def test_camera_lost_for_a_moment_stops_search_and_returns(run, tmp_path, monkeypatch):
    # обрыв посреди записи, к возврату камера снова на связи: возврат сверен, журнал закрыт
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import CameraUnreachable

    original = imaging.ImagingWriter.write

    def write(self, key, value, persist=False):
        if key == "sharpness":
            raise CameraUnreachable("нет ответа (ReadTimeout)")
        return original(self, key, value, persist)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    api = hikvision(password=SECRET)
    result, camera = run(api)
    found = rows(result)
    assert {"API-MAP-1", "API-MATCH-1"} <= set(found)
    search = found["API-SEARCH-1"]
    assert search.result.status is Status.FAILED and "перестала отвечать" in search.result.reason
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    assert 1 in pairs_of(result)  # найденное до обрыва не потеряно
    trace = next(e.content for e in result.evidence if e.name == "search.json")
    assert "не пробовали: поиск прерван на п. 4" in trace
    assert load(tmp_path / "pending-api", "127.0.0.1", camera.port) is None


def test_camera_gone_keeps_map_and_journal_and_is_restore_blocker(run, tmp_path, monkeypatch):
    # камера пропала насовсем: карта и сверка на месте, возврат — блокер, журнал не потерян
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import CameraUnreachable

    api = hikvision(password=SECRET)
    camera = MockCamera(
        Passport("Hikvision", "DS-2CD2043G2-I", "V1", "SN1"), password=SECRET, native=api
    ).start()
    original = imaging.ImagingWriter.write

    def write(self, key, value, persist=False):
        if key == "sharpness":
            camera.stop()
            raise CameraUnreachable("нет ответа (ConnectError)")
        return original(self, key, value, persist)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    monkeypatch.setitem(FAST, "api.reconnect_wait_s", Threshold(0.05, "с", "тест"))
    try:
        result, _ = run(api, camera=camera)
    finally:
        camera.stop()
    found = rows(result)
    assert {"API-MAP-1", "API-MATCH-1"} <= set(found)
    assert found["API-SEARCH-1"].result.status is Status.FAILED
    restore = found["API-RESTORE-1"]
    assert restore.result.status is Status.FAILED and restore.severity is Severity.BLOCKER
    assert "не вернулась за 0.05 с" in restore.result.actual
    entry = load(tmp_path / "pending-api", "127.0.0.1", camera.port)
    assert entry is not None and entry.kind == "4"  # резкость: следующий прогон вернёт по журналу


def _assert_return_fault_is_restore_blocker(result, tmp_path, port, failed_n):
    found = rows(result)
    assert {"API-MAP-1", "API-MATCH-1", "API-SEARCH-1"} <= set(found)
    assert 1 in pairs_of(result)  # пары, найденные до сбоя возврата, не потеряны
    restore = found["API-RESTORE-1"]
    assert restore.result.status is Status.FAILED and restore.severity is Severity.BLOCKER
    assert "возврат по ONVIF не подтверждён" in restore.result.actual
    assert "вернуть вручную" in restore.result.actual
    trace = next(e.content for e in result.evidence if e.name == "search.json")
    # состояние камеры неизвестно: дальше не пишем
    assert f"не пробовали: поиск прерван на п. {failed_n}" in trace
    entry = load(tmp_path / "pending-api", "127.0.0.1", port)
    assert entry is not None and entry.kind == str(failed_n) and entry.stage == "restoring"


def test_onvif_fault_on_image_return_after_reboot_is_restore_blocker(run, tmp_path, monkeypatch):
    # B2 ревью api-discovery: камера перезагрузилась, порт открыт, а ONVIF ещё отвечает 503;
    # раньше Fault на возврате уничтожал всю проверку API-MAP-1
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

    original_write = imaging.ImagingWriter.write
    returns = []

    def write(self, key, value, persist=False):
        if key == "sharpness":
            raise CameraUnreachable("нет ответа (ReadTimeout)")
        return original_write(self, key, value, persist)

    original_restore = imaging.ImagingWriter.restore

    def restore(self, state, touched):
        returns.append(list(touched))
        if touched == ["sharpness"]:
            raise OnvifFault("ответ не SOAP (HTTP 503)", status=503)
        return original_restore(self, state, touched)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    monkeypatch.setattr(imaging.ImagingWriter, "restore", restore)
    result, camera = run(hikvision(password=SECRET))
    _assert_return_fault_is_restore_blocker(result, tmp_path, camera.port, 4)
    assert returns.count(["sharpness"]) == 1  # одна попытка возврата, без повтора


def test_encoder_rejects_return_is_restore_blocker(run, tmp_path, monkeypatch):
    # B2: Fault на возврате конфигурации кодировщика (ForcePersistence, полный блок)
    from qa_tool.drivers.camera import encoder
    from qa_tool.drivers.camera.soap import OnvifFault

    def restore(self, config):
        raise encoder.ValueRejected(OnvifFault("ter:ConfigModify"))

    monkeypatch.setattr(encoder.EncoderWriter, "restore", restore)
    result, camera = run(hikvision(password=SECRET))
    _assert_return_fault_is_restore_blocker(result, tmp_path, camera.port, 15)


def test_password_fault_on_return_is_not_swallowed(run, monkeypatch):
    # отказ пароля на возврате — не итог возврата: вход больше не пробуем (S8)
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import OnvifFault

    def restore(self, state, touched):
        raise OnvifFault("отказ входа", status=401)

    monkeypatch.setattr(imaging.ImagingWriter, "restore", restore)
    result, _ = run(hikvision(password=SECRET))
    assert result.status is Status.STAND_ERROR and "API-RESTORE-1" not in rows(result)


def test_bitrate_edge_like_108_reaches_range_row(run):
    api = dahua(password=SECRET)
    bitrate_edge_like_108(api.state)
    result, _ = run(api, maker="Dahua", model="IPC-HFW2431S-S-S2")
    edge = rows(result)["API-RANGE-15"]
    assert edge.result.status is Status.FAILED and edge.severity is Severity.MAJOR
    assert "держит не больше 1792" in edge.result.actual


def test_without_checkbox_nothing_is_written(tmp_path):
    api = hikvision(password=SECRET)
    camera = MockCamera(
        Passport("Hikvision", "DS", "V1", "SN"), password=SECRET, native=api
    ).start()
    try:
        place = Camera(slot=1, address="127.0.0.1", port=camera.port, username="admin")
        extra = {PASSPORT_KEY: {"manufacturer": "Hikvision", "model": "DS", "firmware": "V1"}}
        result = run_check(SPEC, CheckContext(place, SECRET, Place.COLOR_BOX, extra=extra))
    finally:
        camera.stop()
    assert "API-SEARCH-1" not in rows(result)
    assert api.state.imaging_writes == [] and api.state.encoder_writes == []


def test_shares_pick_far_values():
    assert _shares(0.5, 0.2, 0.8) == [0.2, 0.8]
    assert _shares(0.2, 0.2, 0.8) == [0.8, 0.5]
    assert _shares(0.0, 0.2, 0.8) == [0.8, 0.5]


def test_leaves_of_nested_block():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(
        '<s xmlns:tt="x"><tt:WhiteBalance><tt:Mode>AUTO</tt:Mode></tt:WhiteBalance></s>'
    )
    assert leaves_of(root) == {"WhiteBalance/Mode": "AUTO"}


def test_search_refuses_settings_it_must_not_write():
    from qa_tool.checks.api.catalog import BY_N
    from qa_tool.checks.api.search import Limits, Search

    class Fake:
        entry = BY_N[12]  # экспозиция — защищённый блок

    with pytest.raises(SearchRefused):
        Search(
            [Fake()],
            lambda: None,
            None,
            frozenset(),
            lambda _k: (),
            lambda *a: None,
            Limits(),
            "",
            "",
        )


def test_same_field_at_two_addresses_is_one_parameter():
    from qa_tool.checks.api.search import _groups, _representative
    from qa_tool.core.api_map import Change

    # «Рувер» .100 10.11: яркость в общем разделе и в своей операции сдвигаются вместе
    both = [
        Change("/api/image#/brightness", "50", "20"),
        Change("/api/image/brightness?time_of_day=0#/brightness", "50", "20"),
    ]
    (group,) = _groups(both).values()
    assert _representative(group).id.startswith("/api/image/brightness?")
    # копии Dahua по профилям с разными исходными — тоже один параметр
    copies = [Change(f"VideoColor[0][{i}].Brightness", b, "80") for i, b in enumerate("504")]
    assert len(_groups(copies)) == 1
    # разные поля — разные параметры
    other = [Change("/api/image#/brightness", "50", "20"), Change("/api/image#/gain", "50", "20")]
    assert len(_groups(other)) == 2
