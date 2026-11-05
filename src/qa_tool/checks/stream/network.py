"""STREAM-NET-1: потери и джиттер в сети, по UDP.

TCP прячет потери сети повторной отправкой, поэтому их видно только здесь. UDP не проходит туннели
и может упереться в брандмауэр компьютера стенда: это не вина камеры, такая строка «пропущена».
"""

import json
import time

from qa_tool.checks.stream.common import (
    MAX_PROFILES,
    OPEN_RESERVE_S,
    RTSP_UNAUTHORIZED,
    add_onvif_evidence,
    check_timeout_s,
    client_for,
    limit,
    read_profiles,
    refused_password,
    remember_refusal,
    sample_seconds,
)
from qa_tool.checks.stream.meter import StreamMeter
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Severity,
    Status,
)
from qa_tool.drivers.camera.onvif_generic import MediaProfile
from qa_tool.drivers.camera.rtsp import UDP_BUFFER, RtspClient, RtspError, Track, WatchEnd
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

CHECK_ID = "STREAM-NET-1"
UDP_SILENCE_S = 5.0  # сценарий S6: столько ждём первый пакет, прежде чем винить брандмауэр
RTSP_UNSUPPORTED_TRANSPORT = 461
EXPECTED = "поток идёт по UDP без потерь и джиттера выше порогов"


def _row(profile: MediaProfile, number: int, result: CheckResult) -> ChildResult:
    return ChildResult(
        f"STREAM-NET-{number}",
        f"Поток «{profile.name or profile.token}»: сеть, UDP",
        result,
        f"Выборка потока профиля {profile.token} по RTP поверх UDP.",
        Severity.MAJOR,
    )


def _start_udp(client: RtspClient) -> Track:
    """Запустить видео по UDP.

    Raises:
        OSError, RtspError: поток не открылся; ValueError — в SDP нет видео.
    """
    _, tracks = client.describe()
    track = next((t for t in tracks if t.media == "video"), None)
    if track is None:
        raise ValueError("в описании потока (SDP) нет видеодорожки")
    client.setup(track, udp=True)
    client.play()
    return track


def _verdict(
    ctx: CheckContext, stats: dict, end: WatchEnd, evidence: list[Evidence], buffer: int = 0
) -> CheckResult:
    """Заключение по выборке, в которой пакеты пришли.

    Args:
        ctx: контекст проверки, из него пороги.
        stats: итог `StreamMeter.stats`.
        end: чем кончилось чтение.
        evidence: доказательства строки.
        buffer: фактический буфер приёма UDP, байт; 0 — неизвестен.

    Returns:
        Итог строки.
    """
    loss, jitter = limit(ctx, "stream.udp_loss_pct"), limit(ctx, "stream.jitter_ms")
    problems = []
    if end in (WatchEnd.SILENT, WatchEnd.BROKEN):
        problems.append(f"поток прервался: {end.value}, успел {stats['seconds']:g} с")
    if stats["loss_pct"] > loss.value:
        problems.append(
            f"потеряно {stats['lost']} пакетов ({stats['loss_pct']:g} %, порог {loss.value:g} %)"
        )
    if stats["jitter_ms"] > jitter.value:
        problems.append(
            f"джиттер {stats['jitter_ms']:g} мс, в среднем {stats['jitter_mean_ms']:g} мс, "
            f"порог {jitter.value:g} мс"
        )
    # потери из-за малого буфера — вина компьютера стенда, поэтому читающий должен это видеть
    small = 0 < buffer < UDP_BUFFER
    note = f"; буфер приёма стенда мал: {buffer >> 10} КБ" if small else ""
    return CheckResult(
        Status.FAILED if problems else Status.PASSED,
        reason="; ".join(problems),
        expected=EXPECTED,
        actual=f"потеряно {stats['loss_pct']:g} %, джиттер {stats['jitter_ms']:g} мс{note}",
        evidence=evidence,
        threshold=loss,
    )


def _refused_reason(error: RtspError) -> str:
    if error.status == RTSP_UNSUPPORTED_TRANSPORT:
        return f"камера не отдаёт поток по UDP: {error}"
    return f"поток по UDP не открылся: {error}"


def measure(ctx: CheckContext, profile: MediaProfile, number: int, seconds: float) -> ChildResult:
    """Одна строка протокола на профиль.

    Args:
        ctx: контекст проверки.
        profile: медиапрофиль.
        number: номер профиля с 1.
        seconds: длина выборки, с.

    Returns:
        Строка протокола.
    """
    if not profile.uri.startswith("rtsp://"):
        return _row(
            profile, number, CheckResult.skipped("нет адреса потока: см. строку параметров")
        )
    client = client_for(ctx, profile)
    try:
        with client:
            track = _start_udp(client)
            meter = StreamMeter(track.encoding, track.clock_rate)
            end = client.watch(meter.add, seconds, ctx.stop_requested, UDP_SILENCE_S)
    except RtspError as e:
        if e.status == RTSP_UNAUTHORIZED:
            return _row(profile, number, CheckResult.stand_error(remember_refusal(ctx, e)))
        result = CheckResult(
            Status.FAILED,
            reason=_refused_reason(e),
            expected=EXPECTED,
            evidence=[Evidence(f"rtsp-udp-{number}.txt", "\n".join(client.exchanges))],
        )
        return _row(profile, number, result)
    except (OSError, ValueError) as e:
        return _row(profile, number, CheckResult.stand_error(f"UDP не открылся: {e}"))
    stats = meter.stats()
    if end is WatchEnd.STOPPED:
        return _row(profile, number, CheckResult.skipped("прогон остановлен"))
    if end is WatchEnd.SILENT and not stats["packets"]:
        reason = "UDP не дошёл до стенда: брандмауэр компьютера или туннель; это не ошибка камеры"
        return _row(profile, number, CheckResult.skipped(reason))
    evidence = [
        Evidence(f"stream-udp-{number}.json", json.dumps(stats, ensure_ascii=False, indent=1)),
        Evidence(f"rtsp-udp-{number}.txt", "\n".join(client.exchanges)),
    ]
    return _row(profile, number, _verdict(ctx, stats, end, evidence, client.udp_buffer))


@REGISTRY.check(
    CHECK_ID,
    module="stream",
    title="Сеть: потери и джиттер",
    description="Каждый медиапрофиль читается по RTP поверх UDP: только так видно потери в сети. "
    "Если UDP не дошёл до стенда, строка пропущена: это не ошибка камеры.",
    phase=Phase.NETWORK,
    order=810,
    timeout_s=check_timeout_s(),
    group="Поток",
    help_id="stream",
)
def stream_network(ctx: CheckContext) -> CheckResult:
    """Проверить все медиапрофили по UDP, по одному."""
    try:
        profiles, evidence = read_profiles(ctx)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(Status.FAILED, reason=f"ONVIF: {e}", expected="профили читаются")
    seconds = sample_seconds(ctx)
    deadline = time.monotonic() + MAX_PROFILES * (seconds + OPEN_RESERVE_S)
    rows = []
    for number, profile in enumerate(profiles, start=1):
        reason = ""
        if number > MAX_PROFILES:
            reason = f"профилей больше {MAX_PROFILES}: на этот не хватает срока проверки"
        elif ctx.stop_requested():
            reason = "прогон остановлен"
        elif refused_password(ctx):
            reason = refused_password(ctx)
        elif time.monotonic() + seconds + OPEN_RESERVE_S > deadline:
            reason = "не хватило срока проверки"
        if reason:
            rows.append(_row(profile, number, CheckResult.skipped(reason)))
            continue
        try:
            rows.append(measure(ctx, profile, number, seconds))
        except Exception as e:  # данные одной камеры не должны стоить строк остальных профилей
            result = CheckResult.stand_error(f"сбой проверки профиля: {type(e).__name__}")
            rows.append(_row(profile, number, result))
    add_onvif_evidence(rows, evidence)
    return CheckResult(
        Status.PASSED, actual=f"потоков: {len(profiles)}", evidence=evidence, children=rows
    )
