"""RUV-SD-*: накопитель камеры и свежесть записи по ONVIF — только чтение.

Что ONVIF умеет и чего не умеет (devicemgmt.wsdl, recording.wsdl, search.wsdl):
- GetStorageConfigurations — какие накопители есть (SD-карта, NAS) и их тип;
- объём, свободное место и форматирование в стандарт не входят — их даёт только API
  производителя (адаптер, этап 3), поэтому форматирование «с галочкой разрушающие» живёт там;
- GetRecordingJobs и GetRecordingSummary — включена ли запись и когда записан последний кадр.

Свежесть архива сравнивается с часами самой камеры (GetSystemDateAndTime), а не стенда:
расхождение часов на минуты — обычное дело и не должно давать ложное «не прошла».
"""

import datetime

from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    Evidence,
    Phase,
    Severity,
    Status,
)
from qa_tool.core.models import Threshold
from qa_tool.core.secrets import redact
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_STORAGE = "RUV-SD-1"
CHECK_FRESH = "RUV-SD-2"
FRESH_KEY = "sdcard.fresh_max_s"
DEFAULT_FRESH = Threshold(120.0, "с", "стартовое значение (спека этапа 2)")
NO_CAPACITY = (
    "объём и свободное место ONVIF не передаёт — их покажет адаптер производителя (этап 3)"
)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _text(element, name: str) -> str:
    return next(
        ((e.text or "").strip() for e in element.iter() if _local(e.tag) == name and e.text), ""
    )


def _soap_evidence(soap: SoapClient) -> list[Evidence]:
    return [
        Evidence(f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}")
        for i, x in enumerate(soap.exchanges, start=1)
    ]


def storages(soap: SoapClient, device_url: str) -> list[dict]:
    """Накопители камеры: токен, тип (LocalStorage, NFS, CIFS …), путь или адрес.

    Адрес NAS может содержать логин и пароль — он проходит через redact.
    """
    answer = soap.call(device_url, "<tds:GetStorageConfigurations/>")
    out = []
    for config in (e for e in answer.iter() if _local(e.tag) == "StorageConfigurations"):
        data = next((e for e in config.iter() if _local(e.tag) == "Data"), None)
        out.append(
            {
                "token": config.get("token", ""),
                "type": data.get("type", "") if data is not None else "",
                "where": redact(_text(config, "LocalPath") or _text(config, "StorageUri")),
            }
        )
    return out


def parse_time(text: str) -> datetime.datetime | None:
    """Время ONVIF (xs:dateTime, обычно UTC с «Z») или None, если камера прислала не время."""
    try:
        moment = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)


def camera_now(soap: SoapClient, device_url: str) -> datetime.datetime | None:
    """Текущее время камеры UTC (GetSystemDateAndTime → UTCDateTime)."""
    answer = soap.call(device_url, "<tds:GetSystemDateAndTime/>")
    utc = next((e for e in answer.iter() if _local(e.tag) == "UTCDateTime"), None)
    if utc is None:
        return None
    try:
        parts = [int(_text(utc, n)) for n in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
        return datetime.datetime(*parts, tzinfo=datetime.UTC)
    except ValueError:
        return None


@REGISTRY.check(
    CHECK_STORAGE,
    module="onvif",
    title="Накопитель (SD-карта, NAS)",
    description="Читаем накопители камеры (GetStorageConfigurations). Ожидаем: камера с "
    "записью (Profile G) сообщает хотя бы об одном накопителе и его типе.",
    phase=Phase.NETWORK,
    order=10,
    severity=Severity.MAJOR,
)
def storage(ctx: CheckContext) -> CheckResult:
    """Камера сообщает о накопителе; без записи и без накопителя — «пропущена»."""
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    expected = "камера сообщает о накопителе и его типе"
    try:
        services = service_urls(soap, ctx.camera)
        try:
            found = storages(soap, services["device"])
        except OnvifFault as e:
            found, fault = [], str(e)
        else:
            fault = ""
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED, reason=f"ONVIF: {e}", expected=expected, evidence=_soap_evidence(soap)
        )
    records = "recording" in services
    if not found:
        why = f"GetStorageConfigurations: {fault}" if fault else "список накопителей пуст"
        if not records:
            return CheckResult.skipped(f"камера без записи не сообщает о накопителе ({why})")
        return CheckResult(
            Status.FAILED,
            reason=f"камера записывает (служба записи объявлена), но о накопителе молчит: {why}",
            expected=expected,
            evidence=_soap_evidence(soap),
        )
    actual = "; ".join(f"{s['type'] or 'тип не указан'} {s['where']}".strip() for s in found)
    return CheckResult(
        Status.PASSED,
        expected=expected,
        actual=f"{actual}. {NO_CAPACITY}",
        evidence=_soap_evidence(soap),
    )


@REGISTRY.check(
    CHECK_FRESH,
    module="onvif",
    title="Запись на накопитель идёт",
    description="Если задание записи включено (GetRecordingJobs), сверяем время последнего "
    "записанного кадра (GetRecordingSummary) с часами камеры. Ожидаем: отставание не больше "
    "порога.",
    phase=Phase.NETWORK,
    order=11,
    severity=Severity.MAJOR,
)
def recording_fresh(ctx: CheckContext) -> CheckResult:
    """Последний кадр архива не старше порога, пока задание записи включено."""
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    limit = ctx.thresholds.get(FRESH_KEY, DEFAULT_FRESH)
    expected = f"последний кадр архива не старше {limit.value:g} {limit.unit}"
    try:
        services = service_urls(soap, ctx.camera)
        if not {"recording", "search"} <= services.keys():
            return CheckResult.skipped("камера не объявляет службы записи и поиска (Profile G)")
        jobs = soap.call(services["recording"], "<trc:GetRecordingJobs/>")
        modes = [_text(job, "Mode") for job in jobs.iter() if _local(job.tag) == "JobItem"]
        if "Active" not in modes:
            return CheckResult.skipped(
                "запись на камере не включена: заданий записи в режиме Active нет"
                f" (режимы: {', '.join(modes) or 'заданий нет'})"
            )
        summary = soap.call(services["search"], "<tse:GetRecordingSummary/>")
        now = camera_now(soap, services["device"])
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"ONVIF: {e}",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=limit,
        )
    until = parse_time(_text(summary, "DataUntil"))
    count = _text(summary, "NumberRecordings") or "?"
    evidence = _soap_evidence(soap)
    if now is None:
        return CheckResult(
            Status.FAILED,
            reason="камера не сообщает своё время UTC (GetSystemDateAndTime)",
            expected=expected,
            evidence=evidence,
            threshold=limit,
        )
    if until is None:
        return CheckResult(
            Status.FAILED,
            reason="запись включена, но в сводке архива нет времени последнего кадра",
            expected=expected,
            actual=f"записей: {count}",
            evidence=evidence,
            threshold=limit,
        )
    lag = (now - until).total_seconds()
    actual = (
        f"последний кадр {until:%Y-%m-%d %H:%M:%S} UTC, часы камеры {now:%H:%M:%S}, "
        f"отставание {max(lag, 0):.0f} с, записей: {count}"
    )
    if lag > limit.value:
        return CheckResult(
            Status.FAILED,
            reason=f"запись включена, но архив не пополняется: последний кадр {lag:.0f} с назад",
            expected=expected,
            actual=actual,
            evidence=evidence,
            threshold=limit,
        )
    return CheckResult(
        Status.PASSED, expected=expected, actual=actual, evidence=evidence, threshold=limit
    )
