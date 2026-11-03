"""RUV-G-REPLAY-1: камера реально отдаёт архив по ONVIF Profile G.

Главный пробел готовых средств: камера с одним Profile S показывает картинку в любом
регистраторе, но запись с SD-карты по ONVIF может не отдать. Поэтому архив не только ищется
(FindRecordings → GetRecordingSearchResults) и не только даёт адрес (GetReplayUri), а
**проигрывается** не меньше порога `onvif.replay_min_s` (по умолчанию 10 с) встроенным ffprobe.

Нет записей: с галочкой «изменяющие» программа включает запись по ONVIF на
`replay_record_s` (по умолчанию 120 с; решение директора 2026-11-03) и возвращает задания
записи в исходный режим; без галочки — «пропущена». Сеть и учётные записи не трогаются.

Ограничение до T10: ffprobe не умеет передавать логин и пароль RTSP иначе как в адресе,
поэтому на время проигрывания они видны в списке процессов компьютера стенда; в протокол и
журнал они не попадают (redact). Окончательный способ проигрывания (ffmpeg или свой
RTSP-клиент с заголовком `Require: onvif-replay`) выбирается на живой камере в T10.
"""

import json
import subprocess
import time
from urllib.parse import quote, urlsplit, urlunsplit

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
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient
from qa_tool.tools import tool_path

CHECK_ID = "RUV-G-REPLAY-1"
THRESHOLD_KEY = "onvif.replay_min_s"
DEFAULT_MIN_S = Threshold(10.0, "с", "стартовое значение (спека этапа 2)")
DEFAULT_RECORD_S = 120.0


def _texts(element, name: str) -> list[str]:
    return [(e.text or "").strip() for e in element.iter() if e.tag.endswith("}" + name)]


def find_recordings(soap: SoapClient, search_url: str) -> list[dict]:
    """Записи камеры: токен, начало и конец (ONVIF Recording Search)."""
    token = _texts(
        soap.call(
            search_url,
            "<tse:FindRecordings><tse:Scope/><tse:MaxMatches>20</tse:MaxMatches>"
            "<tse:KeepAliveTime>PT10S</tse:KeepAliveTime></tse:FindRecordings>",
        ),
        "SearchToken",
    )[0]
    found: list[dict] = []
    try:
        for _ in range(5):  # поиск на камере асинхронный: ждём «Completed» не дольше ~25 с
            answer = soap.call(
                search_url,
                f"<tse:GetRecordingSearchResults><tse:SearchToken>{token}</tse:SearchToken>"
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
            search_url, f"<tse:EndSearch><tse:SearchToken>{token}</tse:SearchToken></tse:EndSearch>"
        )
    return [r for r in found if r["token"]]


def record_for_a_while(soap: SoapClient, recording_url: str, seconds: float) -> None:
    """Включить запись (задание в режим Active), подождать и вернуть режимы как были.

    Raises:
        OnvifFault: заданий записи нет или камера не включает запись.
    """
    jobs = soap.call(recording_url, "<trc:GetRecordingJobs/>")
    items = [e for e in jobs.iter() if e.tag.endswith("}JobItem")]
    if not items:
        raise OnvifFault("на камере нет заданий записи (GetRecordingJobs пуст)")
    original = [
        ((_texts(i, "JobToken") or [""])[0], (_texts(i, "Mode") or ["Idle"])[0]) for i in items
    ]
    try:
        for job, _ in original:
            soap.call(
                recording_url,
                f"<trc:SetRecordingJobMode><trc:JobToken>{job}</trc:JobToken>"
                "<trc:Mode>Active</trc:Mode></trc:SetRecordingJobMode>",
            )
        time.sleep(seconds)
    finally:
        for job, mode in original:
            soap.call(
                recording_url,
                f"<trc:SetRecordingJobMode><trc:JobToken>{job}</trc:JobToken>"
                f"<trc:Mode>{mode}</trc:Mode></trc:SetRecordingJobMode>",
            )


def replay_uri(soap: SoapClient, replay_url: str, recording: str) -> str:
    """Адрес RTSP для воспроизведения записи (ONVIF Replay, GetReplayUri)."""
    answer = soap.call(
        replay_url,
        "<trp:GetReplayUri><trp:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trp:StreamSetup>"
        f"<trp:RecordingToken>{recording}</trp:RecordingToken></trp:GetReplayUri>",
    )
    return (_texts(answer, "Uri") or [""])[0]


def play(uri: str, user: str, password: str, seconds: float) -> dict:
    """Проиграть архив ffprobe-ом: длительность по меткам времени, кодек, ключевые кадры.

    Raises:
        FileNotFoundError: нет ffprobe.
        subprocess.CalledProcessError, subprocess.TimeoutExpired: поток не открылся или завис.
    """
    ffprobe = tool_path("ffprobe")
    if ffprobe is None:
        raise FileNotFoundError("ffprobe не найден")
    parts = urlsplit(uri)
    if user and not parts.username:
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.netloc}"
        uri = urlunsplit(parts._replace(netloc=netloc))
    done = subprocess.run(
        # +1 с к порогу: читаем чуть дольше нужного, но не ждём конца архива; -timeout (мкс) —
        # камера, переставшая слать данные, не вешает проверку до общего тайм-аута
        [
            ffprobe,
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "15000000",
            "-read_intervals",
            f"%+{seconds + 1:g}",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            uri,
        ],
        capture_output=True,
        text=True,
        timeout=seconds + 40,
        check=True,
    )
    data = json.loads(done.stdout or "{}")
    times = [float(p["pts_time"]) for p in data.get("packets", []) if p.get("pts_time")]
    stream = (data.get("streams") or [{}])[0]
    return {
        "seconds": round(max(times) - min(times), 2) if times else 0.0,
        "frames": len(times),
        "keyframes": sum("K" in p.get("flags", "") for p in data.get("packets", [])),
        "codec": stream.get("codec_name", ""),
        "size": f"{stream.get('width', '?')}×{stream.get('height', '?')}",
    }


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Выдача архива по ONVIF (Profile G)",
    description="Ищем запись на карте (FindRecordings), получаем адрес воспроизведения "
    "(GetReplayUri) и проигрываем архив. Ожидаем: поток открывается, кадры идут не меньше "
    "порога, есть ключевые кадры.",
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
    expected = f"архив проигрывается ≥ {minimum.value:g} {minimum.unit}, кадры идут"
    try:
        services = service_urls(soap, ctx.camera)
        if not {"search", "replay"} <= services.keys():
            return CheckResult.skipped(
                "камера не объявляет поиск и воспроизведение записей (Profile G)"
            )
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
                    evidence=_soap_evidence(soap),
                )
            record_for_a_while(
                soap,
                services["recording"],
                float(ctx.extra.get("replay_record_s", DEFAULT_RECORD_S)),
            )
            recordings = find_recordings(soap, services["search"])
            if not recordings:
                return CheckResult(
                    Status.FAILED,
                    reason="запись включена, но в поиске не появилась",
                    expected=expected,
                    evidence=_soap_evidence(soap),
                )
        latest = max(recordings, key=lambda r: r["end"])
        uri = replay_uri(soap, services["replay"], latest["token"])
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(
            Status.FAILED,
            reason=f"ONVIF: {e}",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=minimum,
        )
    if not uri.startswith("rtsp://"):
        return CheckResult(
            Status.FAILED,
            reason=f"GetReplayUri вернул не RTSP-адрес: {uri!r}",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=minimum,
        )
    try:
        stats = play(uri, ctx.camera.username, ctx.password, minimum.value)
    except FileNotFoundError as e:
        return CheckResult.stand_error(str(e))
    except subprocess.TimeoutExpired:
        return CheckResult(
            Status.FAILED,
            reason="архив не проигрался: поток завис",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=minimum,
        )
    except (subprocess.CalledProcessError, ValueError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        return CheckResult(
            Status.FAILED,
            reason=f"архив не открывается: {detail.strip()[-300:]}",
            expected=expected,
            evidence=_soap_evidence(soap),
            threshold=minimum,
        )
    actual = (
        f"{stats['seconds']:g} с, кадров {stats['frames']}, ключевых {stats['keyframes']}, "
        f"{stats['codec']} {stats['size']}; запись {latest['token']} "
        f"({latest['start']} — {latest['end']})"
    )
    evidence = [
        *_soap_evidence(soap),
        Evidence("replay.json", json.dumps(stats, ensure_ascii=False)),
    ]
    if stats["seconds"] + 0.5 < minimum.value or not stats["keyframes"]:
        reason = (
            f"архив отдан, но проиграно {stats['seconds']:g} с"
            if stats["keyframes"]
            else "в архиве нет ключевых кадров — изображение не собрать"
        )
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


def _soap_evidence(soap: SoapClient) -> list[Evidence]:
    return [
        Evidence(f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}")
        for i, x in enumerate(soap.exchanges, start=1)
    ]
