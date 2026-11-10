"""Возврат родным API (api-discovery, T16): условия спеки, журнал до отправки, одна попытка."""

from dataclasses import replace

from qa_tool.checks.api.restore import NativeRestorer, old_native_lines
from qa_tool.core.api_map import Change, NativeMap, Section, Sensitivity
from qa_tool.drivers.camera.http import TransportError
from qa_tool.drivers.camera.native import NativeWrite

BLOCK = NativeWrite(
    "PUT", "/ISAPI/Image/channels/1/gain", body="<Gain><GainLevel>100</GainLevel></Gain>"
)


class Collector:
    """Сборщик-заглушка: весь блок Gain возвращается одним PUT, прочее — вручную."""

    def restore_request(self, param, _m0):
        return BLOCK if param.id.startswith("Gain/") else None


def native(**values) -> NativeMap:
    section = Section("Image/channels/1", "картинка", "/ISAPI/Image/channels/1", code=200)
    for name, value in values.items():
        section.add_param(name.replace("__", "/"), "целое", value=value)
    return NativeMap(brand="hikvision", collector="ISAPI", sections=[section])


def restorer(m0, volatile=frozenset(), send=None, log=None):
    log = [] if log is None else log
    sent = []

    def fake_send(write):
        log.append(("send", write.path))
        sent.append(write)
        return 200

    def journal(items):
        log.append(("journal", [i["id"] for i in items]))

    return NativeRestorer(Collector(), m0, volatile, send or fake_send, journal), sent


def test_journal_before_send_and_one_request_per_block():
    m0 = native(Gain__GainLevel="100", Gain__GainMode="auto", Color__brightnessLevel="50")
    now = native(Gain__GainLevel="0", Gain__GainMode="manual", Color__brightnessLevel="50")
    log: list = []
    worker, sent = restorer(m0, log=log)
    changes = [Change("Gain/GainLevel", "100", "0"), Change("Gain/GainMode", "auto", "manual")]
    attempts = worker.run(changes, now)
    assert log[0][0] == "journal" and log[1] == ("send", BLOCK.path)
    assert len(sent) == 1  # оба листа — один PUT блока
    assert all(a.sent and not a.why_not for a in attempts)


def test_no_template_means_manual_without_request():
    m0 = native(Color__brightnessLevel="50")
    now = native(Color__brightnessLevel="80")
    worker, sent = restorer(m0)
    (attempt,) = worker.run([Change("Color/brightnessLevel", "50", "80")], now)
    assert sent == [] and not attempt.sent
    assert attempt.how().startswith("веб-интерфейс камеры, раздел изображения")


def test_block_with_volatile_leaf_is_not_sent():
    # PUT блока вернул бы к старому значению и лист, который меняется сам
    m0 = native(Gain__GainLevel="100", Gain__Current="7")
    now = native(Gain__GainLevel="0", Gain__Current="9")
    worker, sent = restorer(m0, volatile=frozenset({"Gain/Current"}))
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], now)
    assert sent == [] and "меняющийся сам" in attempt.how()


def test_sensitive_or_missing_value_is_manual():
    m0 = native(Gain__GainLevel="100")
    params = m0.sections[0].params
    params[0] = replace(params[0], sensitivity=Sensitivity.SENSITIVE, value=None)
    worker, sent = restorer(m0)
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], native(Gain__GainLevel="0"))
    assert sent == [] and "значения в исходной карте нет" in attempt.how()


def test_transport_error_is_one_attempt_without_retry():
    calls = []

    def broken(write):
        calls.append(write)
        raise TransportError("нет ответа")

    m0 = native(Gain__GainLevel="100")
    worker, _ = restorer(m0, send=broken)
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], native(Gain__GainLevel="0"))
    assert len(calls) == 1 and attempt.sent
    assert "пробовала один раз" in attempt.how() and "TransportError" in attempt.how()


def test_refused_code_is_reported():
    m0 = native(Gain__GainLevel="100")
    worker, _ = restorer(m0, send=lambda _w: 403)
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], native(Gain__GainLevel="0"))
    assert "камера ответила 403" in attempt.how()


def test_xml_body_with_dtd_is_not_sent():
    class Evil(Collector):
        def restore_request(self, param, _m0):
            return NativeWrite("PUT", "/ISAPI/Image/channels/1/gain", body="<!DOCTYPE x []><x/>")

    m0 = native(Gain__GainLevel="100")
    sent = []
    worker = NativeRestorer(
        Evil(), m0, frozenset(), lambda w: sent.append(w) or 200, lambda _i: None
    )
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], native(Gain__GainLevel="0"))
    assert sent == [] and "не разобрано" in attempt.how()


def test_without_send_everything_is_manual():
    m0 = native(Gain__GainLevel="100")
    worker = NativeRestorer(Collector(), m0, frozenset(), None, lambda _i: None)
    (attempt,) = worker.run([Change("Gain/GainLevel", "100", "0")], native(Gain__GainLevel="0"))
    assert not attempt.sent and "записи родным API нет" in attempt.how()


def test_old_native_lines_trusts_nothing():
    good = '[{"id": "Gain/GainLevel", "запрос": "PUT /x", "было": "100", "осталось": "0"}]'
    assert old_native_lines({"native": good}, "ISAPI") == [
        "ISAPI Gain/GainLevel: было 100, осталось 0; запрос возврата: PUT /x"
    ]
    assert old_native_lines({}, "ISAPI") == []
    assert "повреждён" in old_native_lines({"native": "{"}, "ISAPI")[0]
    assert (
        old_native_lines({"native": '[1, "x", {"id": "' + "a" * 999 + '"}]'}, "ISAPI")[0].count("a")
        <= 256
    )
