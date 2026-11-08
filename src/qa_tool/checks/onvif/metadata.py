"""RUV-M-*: метаданные и аналитика ONVIF Profile M — только чтение.

onvif-tt (закреплённая версия) Profile M почти не проверяет: нет ни потока метаданных, ни
сверки модулей аналитики. Три проверки закрывают обязательные функции профиля из ONVIF
Profiles Conformance (они же — строки матрицы `profiles.py`):

- `RUV-M-METADATA-1` — конфигурации метаданных есть и подключены к медиапрофилю;
- `RUV-M-STREAM-1` — поток метаданных медиапрофиля открывается по RTSP, и за порог
  `onvif.metadata_wait_s` приходит хотя бы один документ `tt:MetadataStream`;
- `RUV-M-ANALYTICS-1` — служба аналитики перечисляет поддерживаемые модули, а настроенные
  модули — из их числа.

Ничего не меняется: если камера не шлёт метаданные из-за своей конфигурации, это итог
«не прошла» с подсказкой, а не повод править настройки. Камера без Profile M — «пропущена».
"""

from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

from defusedxml import ElementTree

from qa_tool.checks.onvif.profiles import declared_profiles
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
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, RtspError
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_METADATA = "RUV-M-METADATA-1"
CHECK_STREAM = "RUV-M-STREAM-1"
CHECK_ANALYTICS = "RUV-M-ANALYTICS-1"
WAIT_KEY = "onvif.metadata_wait_s"
DEFAULT_WAIT = Threshold(10.0, "с", "стартовое значение (спека этапа 2)")
METADATA_ENCODING = "vnd.onvif.metadata"  # ONVIF Streaming Specification, раздел о метаданных
MAX_EVIDENCE = 20_000  # символов первого документа: больше в протоколе не читают
NOT_DECLARED = "профиль M не объявлен"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _tokens(element, name: str) -> list[str]:
    return [e.get("token", "") for e in element.iter() if _local(e.tag) == name and e.get("token")]


def _soap_evidence(soap: SoapClient) -> list[Evidence]:
    return [
        Evidence(f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}")
        for i, x in enumerate(soap.exchanges, start=1)
    ]


def _open(ctx: CheckContext) -> tuple[SoapClient, dict[str, str]] | CheckResult:
    """Клиент ONVIF и адреса служб или готовый итог: «пропущена» без Profile M, сбой ONVIF."""
    try:
        if "M" not in declared_profiles(ctx):
            return CheckResult.skipped(NOT_DECLARED)
        soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
        services = service_urls(soap, ctx.camera)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(Status.FAILED, reason=f"ONVIF: {e}")
    if "media2" not in services:
        return CheckResult(
            Status.FAILED,
            reason="профиль M объявлен, но служба Media2 не объявлена (GetServices)",
            evidence=_soap_evidence(soap),
        )
    return soap, services


def metadata_profiles(soap: SoapClient, media2_url: str) -> list[tuple[str, str]]:
    """Медиапрофили с подключённой конфигурацией метаданных: (профиль, конфигурация)."""
    answer = soap.call(
        media2_url, "<tr2:GetProfiles><tr2:Type>Metadata</tr2:Type></tr2:GetProfiles>"
    )
    out = []
    for profile in (e for e in answer.iter() if _local(e.tag) == "Profiles"):
        for token in _tokens(profile, "Metadata"):
            out.append((profile.get("token", ""), token))
    return out


def stream_uri(soap: SoapClient, media2_url: str, profile: str) -> str:
    """Адрес RTSP медиапрофиля (Media2 GetStreamUri)."""
    answer = soap.call(
        media2_url,
        "<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol>"
        f"<tr2:ProfileToken>{escape(profile)}</tr2:ProfileToken></tr2:GetStreamUri>",
    )
    return next(((e.text or "").strip() for e in answer.iter() if _local(e.tag) == "Uri"), "")


DEFAULT_RTSP_PORT = 554


def well_formed(uri: str) -> bool:
    """Разбирается ли адрес от камеры: порт в пределах, скобки IPv6 верные."""
    try:
        return urlsplit(uri).port is not None or bool(urlsplit(uri).hostname)
    except ValueError:
        return False


def to_stand_address(uri: str, address: str, rtsp_port: int) -> str:
    """Адрес потока на хосте камеры со стенда (security.md: только адреса стенда).

    Камера за NAT или с несколькими интерфейсами отдаёт свой внутренний адрес; ходить по нему
    нельзя — подставляем адрес камеры из стенда. Порт из стенда, если он задан не по умолчанию:
    это проброшенный порт, а в ответе камеры — её внутренний. Иначе порт из ответа: камера может
    вещать на нестандартном порту, а в стенде остался 554.
    """
    parts = urlsplit(uri)
    if rtsp_port != DEFAULT_RTSP_PORT:
        return urlunsplit(parts._replace(netloc=f"{address}:{rtsp_port}"))
    try:
        port = parts.port
    except ValueError:  # камера написала порт за пределами 0–65535
        return urlunsplit(parts._replace(netloc=f"{address}:{rtsp_port}"))
    if parts.hostname == address:
        return uri
    return urlunsplit(parts._replace(netloc=f"{address}:{port or rtsp_port}"))


def documents(packets: list[RtpPacket], channel: int) -> list[str]:
    """Документы метаданных из пакетов канала: документ кончается пакетом с маркером RTP."""
    out, current = [], b""
    for packet in packets:
        if packet.channel != channel:
            continue
        current += packet.payload
        if packet.marker:
            out.append(current.decode("utf-8", "replace"))
            current = b""
    return out


def is_metadata_stream(document: str) -> bool:
    """Документ — корректный XML с корнем `MetadataStream` (DTD и ENTITY отвергаются)."""
    try:
        root = ElementTree.fromstring(document.encode())
    except (ElementTree.ParseError, ValueError):
        return False
    return _local(root.tag) == "MetadataStream"


@REGISTRY.check(
    CHECK_METADATA,
    module="onvif",
    title="Конфигурация метаданных (Profile M)",
    description="Читаем конфигурации метаданных (GetMetadataConfigurations) и медиапрофили "
    "(GetProfiles). Ожидаем: конфигурация есть и подключена хотя бы к одному медиапрофилю.",
    phase=Phase.NETWORK,
    order=30,
    severity=Severity.MAJOR,
    group="Профиль M: метаданные и аналитика",
    help_id="onvif-metadata",
)
def metadata_configuration(ctx: CheckContext) -> CheckResult:
    """Конфигурации метаданных есть и подключены к медиапрофилю."""
    opened = _open(ctx)
    if isinstance(opened, CheckResult):
        return opened
    soap, services = opened
    expected = "есть конфигурация метаданных, она подключена к медиапрофилю"
    try:
        answer = soap.call(services["media2"], "<tr2:GetMetadataConfigurations/>")
        configs = _tokens(answer, "Configurations")
        linked = metadata_profiles(soap, services["media2"])
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED, reason=f"ONVIF: {e}", expected=expected, evidence=_soap_evidence(soap)
        )
    actual = (
        f"конфигураций: {len(configs)} ({', '.join(configs) or '—'}); "
        f"в медиапрофилях: {', '.join(f'{p} ← {c}' for p, c in linked) or 'нет'}"
    )
    if not configs:
        reason = "камера не отдаёт ни одной конфигурации метаданных"
    elif not linked:
        reason = "конфигурации метаданных есть, но ни один медиапрофиль их не использует"
    else:
        return CheckResult(
            Status.PASSED, expected=expected, actual=actual, evidence=_soap_evidence(soap)
        )
    return CheckResult(
        Status.FAILED,
        reason=reason,
        expected=expected,
        actual=actual,
        evidence=_soap_evidence(soap),
    )


@REGISTRY.check(
    CHECK_STREAM,
    module="onvif",
    title="Поток метаданных RTSP (Profile M)",
    description="Открываем по RTSP поток медиапрофиля с метаданными, подключаем дорожку "
    "vnd.onvif.metadata и читаем её. Ожидаем: за порог приходит хотя бы один документ "
    "MetadataStream.",
    phase=Phase.NETWORK,
    order=31,
    severity=Severity.MAJOR,
    group="Профиль M: метаданные и аналитика",
    help_id="onvif-metadata",
    timeout_s=90,
)
def metadata_stream(ctx: CheckContext) -> CheckResult:
    """Поток метаданных открывается и в нём приходят документы MetadataStream."""
    opened = _open(ctx)
    if isinstance(opened, CheckResult):
        return opened
    soap, services = opened
    wait = ctx.thresholds.get(WAIT_KEY, DEFAULT_WAIT)
    expected = f"документ MetadataStream приходит за ≤ {wait.value:g} {wait.unit}"
    try:
        linked = metadata_profiles(soap, services["media2"])
        if not linked:
            return CheckResult(
                Status.FAILED,
                reason="нет медиапрофиля с метаданными — поток открыть нечем",
                expected=expected,
                evidence=_soap_evidence(soap),
                threshold=wait,
            )
        profile = linked[0][0]
        uri = stream_uri(soap, services["media2"], profile)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"ONVIF: {e}",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=wait,
        )
    evidence = _soap_evidence(soap)
    if not uri.startswith("rtsp://") or not well_formed(uri):
        return CheckResult(
            Status.FAILED,
            reason=f"GetStreamUri профиля {profile} вернул не RTSP-адрес: {uri!r}",
            expected=expected,
            evidence=evidence,
            threshold=wait,
        )
    client = RtspClient(
        to_stand_address(uri, ctx.camera.address, ctx.camera.rtsp_port),
        ctx.camera.username,
        ctx.password,
        timeout=10,
    )
    try:
        with client:
            sdp, tracks = client.describe()
            evidence.append(Evidence("sdp.txt", sdp))
            track = next((t for t in tracks if t.encoding.lower() == METADATA_ENCODING), None)
            if track is None:
                return CheckResult(
                    Status.FAILED,
                    reason=f"в потоке профиля {profile} нет дорожки {METADATA_ENCODING}",
                    expected=expected,
                    actual="дорожки: " + ", ".join(f"{t.media} {t.encoding}" for t in tracks),
                    evidence=evidence,
                    threshold=wait,
                )
            client.setup(track, channel=0)
            client.play()
            docs = documents(client.read_packets(wait.value), channel=0)
    except (OSError, RtspError) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"поток метаданных не открывается: {e}",
            expected=expected,
            evidence=evidence,
            threshold=wait,
        )
    valid = [d for d in docs if is_metadata_stream(d)]
    if docs:
        evidence.append(Evidence("metadata-1.xml", docs[0][:MAX_EVIDENCE]))
    actual = f"за {wait.value:g} с документов: {len(docs)}, из них MetadataStream: {len(valid)}"
    if not valid:
        reason = (
            "документы метаданных не разбираются как MetadataStream"
            if docs
            else f"за {wait.value:g} с не пришло ни одного документа метаданных; проверьте, "
            "что в конфигурации метаданных камеры включены аналитика или события"
        )
        return CheckResult(
            Status.FAILED,
            reason=reason,
            expected=expected,
            actual=actual,
            evidence=evidence,
            threshold=wait,
        )
    return CheckResult(
        Status.PASSED, expected=expected, actual=actual, evidence=evidence, threshold=wait
    )


@REGISTRY.check(
    CHECK_ANALYTICS,
    module="onvif",
    title="Модули аналитики (Profile M)",
    description="Читаем конфигурации аналитики (GetAnalyticsConfigurations), поддерживаемые "
    "(GetSupportedAnalyticsModules) и настроенные (GetAnalyticsModules) модули. Ожидаем: "
    "поддерживаемые модули перечисляются, настроенные — из их числа.",
    phase=Phase.NETWORK,
    order=32,
    severity=Severity.MAJOR,
    group="Профиль M: метаданные и аналитика",
    help_id="onvif-metadata",
)
def analytics_modules(ctx: CheckContext) -> CheckResult:
    """Служба аналитики перечисляет модули; настроенные модули — из поддерживаемых."""
    opened = _open(ctx)
    if isinstance(opened, CheckResult):
        return opened
    soap, services = opened
    expected = "поддерживаемые модули перечисляются, настроенные — из их числа"
    if "analytics" not in services:
        return CheckResult(
            Status.FAILED,
            reason="профиль M объявлен, но служба аналитики не объявлена (GetServices)",
            expected=expected,
            evidence=_soap_evidence(soap),
        )
    problems, lines = [], []
    try:
        answer = soap.call(services["media2"], "<tr2:GetAnalyticsConfigurations/>")
        configs = _tokens(answer, "Configurations")
        for token in configs:
            body = f"<tan:ConfigurationToken>{escape(token)}</tan:ConfigurationToken>"
            supported = _module_types(
                soap.call(
                    services["analytics"],
                    f"<tan:GetSupportedAnalyticsModules>{body}</tan:GetSupportedAnalyticsModules>",
                ),
                "AnalyticsModuleDescription",
            )
            configured = _module_types(
                soap.call(
                    services["analytics"],
                    f"<tan:GetAnalyticsModules>{body}</tan:GetAnalyticsModules>",
                ),
                "AnalyticsModule",
            )
            unknown = sorted(set(configured) - set(supported))
            lines.append(
                f"{token}: поддерживает {', '.join(supported) or '—'}; "
                f"настроены {', '.join(configured) or '—'}"
            )
            if not supported:
                problems.append(f"{token}: камера не перечисляет поддерживаемые модули")
            if unknown:
                problems.append(f"{token}: настроены неподдерживаемые модули {', '.join(unknown)}")
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED, reason=f"ONVIF: {e}", expected=expected, evidence=_soap_evidence(soap)
        )
    if not configs:
        problems.append("камера не отдаёт ни одной конфигурации аналитики")
    actual = "; ".join(lines) or "конфигураций аналитики нет"
    status = Status.FAILED if problems else Status.PASSED
    return CheckResult(
        status,
        reason="; ".join(problems),
        expected=expected,
        actual=actual,
        evidence=_soap_evidence(soap),
    )


def _module_types(answer, element: str) -> list[str]:
    """Типы модулей без префикса пространства имён: камеры пишут tt:, tt2: или свой."""
    names = []
    for e in answer.iter():
        if _local(e.tag) == element:
            kind = e.get("Type") or e.get("Name") or ""
            names.append(kind.split(":")[-1])
    return [n for n in names if n]
