"""RUV-G-REPLAY-1: камера реально отдаёт архив по ONVIF Profile G.

Главный пробел готовых средств: камера с одним Profile S показывает картинку в любом
регистраторе, но запись с SD-карты по ONVIF может не отдать. Поэтому архив не только ищется
(FindRecordings → GetRecordingSearchResults) и не только даёт адрес (GetReplayUri), а
**проигрывается** не меньше порога `onvif.replay_min_s` (по умолчанию 10 с).

Нет записей: с галочкой «изменяющие» программа включает запись по ONVIF на
`replay_record_s` (по умолчанию 120 с; решение директора 2026-11-03) и возвращает задания
записи в исходный режим; без галочки — «пропущена». Сеть и учётные записи не трогаются.

Проигрывает свой RTSP-клиент (`drivers/camera/rtsp.py`), а не ffprobe: архиву ONVIF нужен
заголовок `Require: onvif-replay` и время в `Range: clock=…` (ONVIF Streaming Specification,
раздел о воспроизведении), а ffprobe передаёт пароль только в адресе — тогда он виден в списке
процессов компьютера стенда. Камера, которая заголовок не понимает (551), получает повтор без
него. Длительность считается по меткам времени RTP, ключевые кадры — по типу кадра в видео и
по признаку ключевого кадра в расширении заголовка RTP архива ONVIF.
"""

import datetime
import json
import time
from collections.abc import Iterator
from xml.sax.saxutils import escape

from qa_tool.checks.onvif.metadata import to_stand_address
from qa_tool.checks.onvif.storage import parse_time
from qa_tool.checks.rtsp_refusal import refusal_result, refused_password
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
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, RtspError, Track
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_ID = "RUV-G-REPLAY-1"
THRESHOLD_KEY = "onvif.replay_min_s"
DEFAULT_MIN_S = Threshold(10.0, "с", "стартовое значение (спека этапа 2)")
DEFAULT_RECORD_S = 120.0
REPLAY_HEADERS = {"Require": "onvif-replay"}
OPTION_NOT_SUPPORTED = 551  # RFC 2326 §11.3.14: камера не знает заголовка Require
ONVIF_EXTENSION = b"\xab\xac"  # профиль расширения RTP архива ONVIF: NTP-время и флаги C/E/D
START_DELAY_S = 20.0  # запас на поиск места в архиве камерой до первого кадра
TIME_SLACK_S = 60.0  # допуск времени кадров к границам записи: GOP до запрошенного места и рост
NTP_EPOCH = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)


def _texts(element, name: str) -> list[str]:
    return [(e.text or "").strip() for e in element.iter() if e.tag.endswith("}" + name)]


def find_recordings(soap: SoapClient, search_url: str) -> list[dict]:
    """Записи камеры: токен, начало и конец (ONVIF Recording Search).

    Raises:
        OnvifFault: камера не начала поиск (нет SearchToken) — это её ошибка, а не стенда.
    """
    tokens = _texts(
        soap.call(
            search_url,
            "<tse:FindRecordings><tse:Scope/><tse:MaxMatches>20</tse:MaxMatches>"
            "<tse:KeepAliveTime>PT10S</tse:KeepAliveTime></tse:FindRecordings>",
        ),
        "SearchToken",
    )
    if not tokens or not tokens[0]:
        raise OnvifFault("FindRecordings не вернул SearchToken")
    token = tokens[0]
    found: list[dict] = []
    try:
        for _ in range(5):  # поиск на камере асинхронный: ждём «Completed» не дольше ~25 с
            answer = soap.call(
                search_url,
                f"<tse:GetRecordingSearchResults><tse:SearchToken>{escape(token)}</tse:SearchToken>"
                "<tse:MinResults>1</tse:MinResults><tse:WaitTime>PT5S</tse:WaitTime>"
                "</tse:GetRecordingSearchResults>",
            )
            for info in (e for e in answer.iter() if e.tag.endswith("}RecordingInformation")):
                found.append(
                    {
                        "token": (_texts(info, "RecordingToken") or [""])[0],
                        "start": (_texts(info, "EarliestRecording") or [""])[0],
                        "end": (_texts(info, "LatestRecording") or [""])[0],
                    }
                )
            if "Completed" in _texts(answer, "SearchState"):
                break
    finally:
        soap.call(
            search_url,
            f"<tse:EndSearch><tse:SearchToken>{escape(token)}</tse:SearchToken></tse:EndSearch>",
        )
    return [r for r in found if r["token"]]


def _job_modes(soap: SoapClient, recording_url: str) -> list[tuple[str, str]]:
    jobs = soap.call(recording_url, "<trc:GetRecordingJobs/>")
    items = [e for e in jobs.iter() if e.tag.endswith("}JobItem")]
    return [((_texts(i, "JobToken") or [""])[0], (_texts(i, "Mode") or ["Idle"])[0]) for i in items]


def _set_mode(soap: SoapClient, recording_url: str, job: str, mode: str) -> None:
    soap.call(
        recording_url,
        f"<trc:SetRecordingJobMode><trc:JobToken>{escape(job)}</trc:JobToken>"
        f"<trc:Mode>{escape(mode)}</trc:Mode></trc:SetRecordingJobMode>",
    )


def record_for_a_while(soap: SoapClient, recording_url: str, seconds: float) -> list[str]:
    """Включить запись (задания в режим Active), подождать, вернуть режимы и сверить.

    Каждое задание возвращается отдельно: сбой одного не оставляет остальные включёнными
    (never.md — камера не уходит со стенда изменённой).

    Returns:
        Что не удалось вернуть («задание X: осталось Active, было Idle»); пусто — всё вернулось.

    Raises:
        OnvifFault: заданий записи нет.
        CameraUnreachable, OnvifFault: камера не включает запись, а режимы вернулись и сверены.
            Если не вернулись, отказ включения идёт первой строкой в результат: камера осталась
            изменённой, и это важнее отказа.
    """
    original = _job_modes(soap, recording_url)
    if not original:
        raise OnvifFault("на камере нет заданий записи (GetRecordingJobs пуст)")
    problems: list[str] = []
    refused: CameraUnreachable | OnvifFault | None = None
    try:
        for job, _ in original:
            _set_mode(soap, recording_url, job, "Active")
        time.sleep(seconds)
    except (CameraUnreachable, OnvifFault) as e:
        refused = e
    finally:
        for job, mode in original:
            try:
                _set_mode(soap, recording_url, job, mode)
            except (CameraUnreachable, OnvifFault) as e:
                problems.append(f"задание {job}: режим {mode} не вернулся ({e})")
        try:
            now = dict(_job_modes(soap, recording_url))
        except (CameraUnreachable, OnvifFault) as e:
            problems.append(f"режимы заданий записи не сверить: {e}")
        else:
            problems += [
                f"задание {job}: осталось {now[job]}, было {mode}"
                for job, mode in original
                if job in now and now[job] != mode
            ]
    if refused is not None:
        if not problems:
            raise refused
        problems.insert(0, f"запись не включилась: {refused}")
    return problems


def replay_uri(soap: SoapClient, replay_url: str, recording: str) -> str:
    """Адрес RTSP для воспроизведения записи (ONVIF Replay, GetReplayUri)."""
    answer = soap.call(
        replay_url,
        "<trp:GetReplayUri><trp:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trp:StreamSetup>"
        f"<trp:RecordingToken>{escape(recording)}</trp:RecordingToken></trp:GetReplayUri>",
    )
    return (_texts(answer, "Uri") or [""])[0]


def _aggregated(payload: bytes, offset: int) -> Iterator[int]:
    """Первый байт каждого NAL агрегации: перед NAL размер в 2 байта; длины недоверенные."""
    while offset + 2 < len(payload):
        size = int.from_bytes(payload[offset : offset + 2], "big")
        if not size:
            return
        yield payload[offset + 2]
        offset += 2 + size


def is_keyframe(encoding: str, payload: bytes) -> bool:
    """Пакет несёт начало ключевого кадра (H.264 IDR, H.265 IRAP, любой кадр JPEG).

    Разбирается одиночный NAL, начало FU (RFC 6184 §5.8, RFC 7798 §4.4.3) и все NAL агрегации
    (STAP-A, AP): камеры кладут в неё SPS и PPS вместе с маленьким ключевым кадром.
    """
    codec = encoding.upper()
    if codec == "JPEG":
        return True
    if codec == "H264" and payload:
        kind = payload[0] & 0x1F
        if kind == 28 and len(payload) > 1:  # FU-A: важен только первый фрагмент
            return bool(payload[1] & 0x80) and payload[1] & 0x1F == 5
        if kind == 24:  # STAP-A
            return any(nal & 0x1F == 5 for nal in _aggregated(payload, 1))
        return kind == 5
    if codec in ("H265", "HEVC") and len(payload) > 1:
        kind = (payload[0] >> 1) & 0x3F
        if kind == 49 and len(payload) > 2:  # FU
            return bool(payload[2] & 0x80) and 16 <= payload[2] & 0x3F <= 21
        if kind == 48:  # AP
            return any(16 <= (nal >> 1) & 0x3F <= 21 for nal in _aggregated(payload, 2))
        return 16 <= kind <= 21
    return False


def onvif_frame_info(extension: bytes) -> tuple[datetime.datetime, bool] | None:
    """Время кадра (NTP, UTC) и признак ключевого кадра из расширения RTP архива ONVIF."""
    if len(extension) < 13 or extension[:2] != ONVIF_EXTENSION:
        return None
    seconds, fraction = int.from_bytes(extension[4:8]), int.from_bytes(extension[8:12])
    moment = NTP_EPOCH + datetime.timedelta(seconds=seconds + fraction / 2**32)
    return moment, bool(extension[12] & 0x80)


def summarize(packets: list[RtpPacket], track: Track, channel: int) -> dict:
    """Итог проигрывания дорожки: длительность по меткам RTP, кадры, ключевые, время архива."""
    video = [p for p in packets if p.channel == channel]
    elapsed, previous = 0, None
    for packet in video:
        if previous is not None:
            step = (packet.timestamp - previous) % 2**32
            if step < 2**31:  # метки идут вперёд; «назад» — переупорядоченный пакет
                elapsed += step
        previous = packet.timestamp
    keyframes = 0
    moments = []
    for packet in video:
        info = onvif_frame_info(packet.extension)
        if info:
            moments.append(info[0])
        if (info and info[1]) or is_keyframe(track.encoding, packet.payload):
            keyframes += 1
    stats = {
        "seconds": round(elapsed / track.clock_rate, 2),
        "frames": sum(p.marker for p in video),
        "keyframes": keyframes,
        "codec": track.encoding.lower(),
    }
    if moments:
        stats["archive"] = f"{min(moments):%Y-%m-%d %H:%M:%S} — {max(moments):%H:%M:%S} UTC"
        stats["first_utc"], stats["last_utc"] = min(moments).isoformat(), max(moments).isoformat()
    return stats


def outside_recording(stats: dict, recording: dict) -> str:
    """Почему время кадров не совпадает с записью; пусто — совпадает.

    Время кадра архива ONVIF передаёт в расширении RTP — без него регистратор не покажет, к
    какому моменту относится кадр, поэтому отсутствие метки — тоже ошибка. Допуск
    `TIME_SLACK_S`: камера начинает с ключевого кадра до запрошенного места, а запись,
    пока идёт проверка, продолжает расти.
    """
    if "first_utc" not in stats:
        return "в пакетах архива нет времени кадров (расширение RTP ONVIF 0xABAC)"
    begin, end = parse_time(recording["start"]), parse_time(recording["end"])
    first = datetime.datetime.fromisoformat(stats["first_utc"])
    last = datetime.datetime.fromisoformat(stats["last_utc"])
    slack = datetime.timedelta(seconds=TIME_SLACK_S)
    if (begin and first < begin - slack) or (end and last > end + slack):
        return (
            f"время кадров ({stats['archive']}) вне записи "
            f"({recording['start']} — {recording['end']}): "
            "камера отдаёт не тот архив или живой поток"
        )
    return ""


def play(client: RtspClient, seconds: float, start: datetime.datetime | None) -> tuple[str, dict]:
    """Проиграть архив: видеодорожка с места `start`, пока не наберётся `seconds` секунд.

    Returns:
        SDP потока и итог (`summarize`).

    Raises:
        OSError, RtspError: поток не открылся; ValueError — в архиве нет видеодорожки.
    """
    try:
        sdp, tracks = client.describe()
    except RtspError as e:
        if e.status != OPTION_NOT_SUPPORTED:
            raise
        client.extra_headers.pop("Require", None)
        sdp, tracks = client.describe()
    track = next((t for t in tracks if t.media == "video"), None)
    if track is None:
        raise ValueError("в описании архива (SDP) нет видеодорожки")
    client.setup(track, channel=0)
    # время в Range — абсолютное (clock), как требует ONVIF для архива; без него — с начала
    client.play({"Range": f"clock={start:%Y%m%dT%H%M%SZ}-"} if start else None)
    goal = (seconds + 0.5) * track.clock_rate
    clock = {"elapsed": 0, "last": None}

    def enough(packet: RtpPacket) -> bool:
        if packet.channel != client.rtp_channel:
            return False
        if clock["last"] is not None:
            step = (packet.timestamp - clock["last"]) % 2**32
            clock["elapsed"] += step if step < 2**31 else 0
        clock["last"] = packet.timestamp
        return packet.marker and clock["elapsed"] >= goal

    packets = client.read_packets(seconds + START_DELAY_S, limit=200_000, until=enough)
    return sdp, summarize(packets, track, client.rtp_channel)


def start_point(recording: dict, seconds: float) -> datetime.datetime | None:
    """Откуда проигрывать: за `seconds` + 10 с до конца записи, но не раньше её начала."""
    begin, end = parse_time(recording["start"]), parse_time(recording["end"])
    if end is None:
        return begin
    point = end - datetime.timedelta(seconds=seconds + 10)
    return max(point, begin) if begin else point


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Выдача архива по ONVIF (Profile G)",
    description="Ищем запись на карте (FindRecordings), получаем адрес воспроизведения "
    "(GetReplayUri) и проигрываем архив. Ожидаем: поток открывается, кадры идут не меньше "
    "порога, есть ключевые кадры, время кадров — в пределах записи.",
    phase=Phase.NETWORK,
    order=20,
    severity=Severity.BLOCKER,
    timeout_s=DEFAULT_RECORD_S + 180,
    group="Профиль G: накопитель и архив",
    help_id="onvif-archive",
)
def replay(ctx: CheckContext) -> CheckResult:
    """Найти, получить и проиграть архив; при пустой карте — записать (если разрешено)."""
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    minimum = ctx.thresholds.get(THRESHOLD_KEY, DEFAULT_MIN_S)
    expected = (
        f"архив проигрывается ≥ {minimum.value:g} {minimum.unit}, кадры идут, "
        "время кадров — в пределах записи"
    )
    try:
        foreign: list[str] = []
        services = service_urls(soap, ctx.camera, foreign)
        if not {"search", "replay"} <= services.keys():
            reason = "камера не объявляет поиск и воспроизведение записей (Profile G)"
            if foreign:
                reason += f"; службы на чужом хосте отброшены: {', '.join(foreign)}"
            return CheckResult.skipped(reason)
        recordings = find_recordings(soap, services["search"])
        if not recordings:
            if not ctx.allow_writes:
                return CheckResult.skipped(
                    "на карте нет записей, включить запись не разрешено сценарием"
                )
            if "recording" not in services:
                return CheckResult(
                    Status.FAILED,
                    reason="нет записей и нет службы управления записью",
                    expected=expected,
                    evidence=soap_evidence(soap),
                )
            left_changed = record_for_a_while(
                soap,
                services["recording"],
                # дольше планировщик снимет процесс, и режимы записи не вернутся
                min(float(ctx.extra.get("replay_record_s", DEFAULT_RECORD_S)), DEFAULT_RECORD_S),
            )
            if left_changed:
                return CheckResult(
                    Status.FAILED,
                    reason="камера осталась изменённой: " + "; ".join(left_changed),
                    expected=expected,
                    evidence=soap_evidence(soap),
                )
            recordings = find_recordings(soap, services["search"])
            if not recordings:
                return CheckResult(
                    Status.FAILED,
                    reason="запись включена, но в поиске не появилась",
                    expected=expected,
                    evidence=soap_evidence(soap),
                )
        latest = max(recordings, key=lambda r: r["end"])
        uri = replay_uri(soap, services["replay"], latest["token"])
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"ONVIF: {e}",
            expected=expected,
            evidence=soap_evidence(soap),
            threshold=minimum,
        )
    evidence = soap_evidence(soap)
    if not uri.startswith("rtsp://"):
        return CheckResult(
            Status.FAILED,
            reason=f"GetReplayUri вернул не RTSP-адрес: {uri!r}",
            expected=expected,
            evidence=evidence,
            threshold=minimum,
        )
    if refused_password(ctx):
        return CheckResult.skipped(refused_password(ctx))
    client = RtspClient(
        to_stand_address(uri, ctx.camera.address, ctx.camera.rtsp_port),
        ctx.camera.username,
        ctx.password,
        timeout=10,
        extra_headers=dict(REPLAY_HEADERS),
    )
    try:
        with client:
            sdp, stats = play(client, minimum.value, start_point(latest, minimum.value))
        evidence.append(Evidence("sdp.txt", sdp))
    except (OSError, RtspError, ValueError) as e:
        return refusal_result(ctx, e) or CheckResult(
            Status.FAILED,
            reason=f"архив не открывается: {e}",
            expected=expected,
            evidence=[*evidence, Evidence("rtsp.txt", "\n".join(client.exchanges))],
            threshold=minimum,
        )
    evidence += [
        Evidence("rtsp.txt", "\n".join(client.exchanges)),
        Evidence("replay.json", json.dumps(stats, ensure_ascii=False)),
    ]
    actual = (
        f"{stats['seconds']:g} с, кадров {stats['frames']}, ключевых {stats['keyframes']}, "
        f"{stats['codec']}; запись {latest['token']} ({latest['start']} — {latest['end']})"
    )
    if "archive" in stats:
        actual += f"; время кадров архива {stats['archive']}"
    mismatch = outside_recording(stats, latest)
    if stats["seconds"] + 0.5 < minimum.value or not stats["keyframes"] or mismatch:
        if not stats["keyframes"]:
            reason = "в архиве нет ключевых кадров — изображение не собрать"
        elif stats["seconds"] + 0.5 < minimum.value:
            reason = f"архив отдан, но проиграно {stats['seconds']:g} с"
        else:
            reason = mismatch
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


def soap_evidence(soap: SoapClient) -> list[Evidence]:
    """Обмен SOAP с камерой — по файлу доказательства на запрос (секреты вырезает RunStore)."""
    return [
        Evidence(f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}")
        for i, x in enumerate(soap.exchanges, start=1)
    ]
