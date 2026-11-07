"""RUV-S-STREAM-1: живой видеопоток медиапрофиля открывается по RTSP и в нём идут кадры.

Заменяет тесты onvif-tt с ffprobe (excluded.py): те передают пароль камеры в командной строке
и ходят на хост из ответа камеры. Здесь — свой RTSP-клиент: пароль только в заголовке
Authorization, адрес потока — на хосте камеры из стенда, методы — только чтение (табу).
Глубокий разбор потока (потери, частота кадров, битрейт) — модуль «Поток» этапа 4.
"""

from xml.sax.saxutils import escape

from qa_tool.checks.onvif.metadata import to_stand_address, well_formed
from qa_tool.checks.onvif.replay import frames_problem, play, soap_evidence
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
from qa_tool.drivers.camera.rtsp import RtspClient, RtspError
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_ID = "RUV-S-STREAM-1"
THRESHOLD_KEY = "onvif.stream_min_s"
DEFAULT_MIN_S = Threshold(5.0, "с", "стартовое значение: хватает на 1–2 ключевых кадра")


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def first_stream(soap: SoapClient, services: dict[str, str]) -> tuple[str, str]:
    """Первый медиапрофиль и его адрес RTSP: Media2, если объявлен, иначе Media.

    Raises:
        OnvifFault: нет службы медиа или медиапрофилей.
    """
    if "media2" in services:
        url, prefix, setup = services["media2"], "tr2", "<tr2:Protocol>RTSP</tr2:Protocol>"
    elif "media" in services:
        url, prefix = services["media"], "trt"
        setup = (
            "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
            "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
        )
    else:
        raise OnvifFault("камера не объявляет службу медиа (Media или Media2)")
    answer = soap.call(url, f"<{prefix}:GetProfiles/>")
    profile = next((e.get("token", "") for e in answer.iter() if _local(e.tag) == "Profiles"), "")
    if not profile:
        raise OnvifFault("GetProfiles не вернул ни одного медиапрофиля")
    answer = soap.call(
        url,
        f"<{prefix}:GetStreamUri>{setup}"
        f"<{prefix}:ProfileToken>{escape(profile)}</{prefix}:ProfileToken>"
        f"</{prefix}:GetStreamUri>",
    )
    uri = next(((e.text or "").strip() for e in answer.iter() if _local(e.tag) == "Uri"), "")
    return profile, uri


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Видеопоток RTSP",
    description="Получаем адрес потока первого медиапрофиля (GetStreamUri) и открываем его по "
    "RTSP поверх TCP. Ожидаем: видеокадры идут не меньше порога, есть ключевой кадр.",
    phase=Phase.NETWORK,
    order=5,
    severity=Severity.BLOCKER,
    timeout_s=90,
    group="Видеопоток",
    help_id="onvif-stream",
)
def video_stream(ctx: CheckContext) -> CheckResult:
    """Поток первого медиапрофиля открывается, кадры и ключевые кадры идут."""
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    minimum = ctx.thresholds.get(THRESHOLD_KEY, DEFAULT_MIN_S)
    expected = f"видео идёт ровно ≥ {minimum.value:g} {minimum.unit}, есть ключевой кадр"
    try:
        profile, uri = first_stream(soap, service_urls(soap, ctx.camera))
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"ONVIF: {e}",
            expected=expected,
            evidence=soap_evidence(soap),
            threshold=minimum,
        )
    evidence = soap_evidence(soap)
    if not uri.startswith("rtsp://") or not well_formed(uri):
        return CheckResult(
            Status.FAILED,
            reason=f"GetStreamUri профиля {profile} вернул не RTSP-адрес: {uri!r}",
            expected=expected,
            evidence=evidence,
            threshold=minimum,
        )
    client = RtspClient(
        to_stand_address(uri, ctx.camera.address, ctx.camera.rtsp_port),
        ctx.camera.username,
        ctx.password,
        timeout=10,
    )
    try:
        with client:
            sdp, stats = play(client, minimum.value, None)
        evidence.append(Evidence("sdp.txt", sdp))
    except (OSError, RtspError, ValueError) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"поток профиля {profile} не открывается: {e}",
            expected=expected,
            evidence=[*evidence, Evidence("rtsp.txt", "\n".join(client.exchanges))],
            threshold=minimum,
        )
    evidence.append(Evidence("rtsp.txt", "\n".join(client.exchanges)))
    actual = (
        f"профиль {profile}: {stats['seconds']:g} с, кадров {stats['frames']}, "
        f"ключевых {stats['keyframes']}, {stats['codec']}"
    )
    uneven = frames_problem(ctx, stats)
    if stats["seconds"] + 0.5 < minimum.value or not stats["keyframes"] or uneven:
        if not stats["keyframes"]:
            reason = "в потоке нет ключевых кадров — изображение не собрать"
        elif stats["seconds"] + 0.5 < minimum.value:
            reason = f"поток открылся, но видео шло {stats['seconds']:g} с"
        else:
            reason = uneven
        return CheckResult(
            Status.FAILED,
            reason=reason,
            expected=expected,
            actual=actual,
            evidence=evidence,
            threshold=minimum,
        )
    return CheckResult(
        Status.PASSED, expected=expected, actual=actual, evidence=evidence, threshold=minimum
    )
