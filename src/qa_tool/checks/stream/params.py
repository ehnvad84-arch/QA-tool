"""STREAM-PARAMS-1: каждый медиапрофиль отдаёт то, что заявляет, без потерь и ошибок декодера.

Только чтение: настройки камеры не меняются. На профиль три строки протокола, у каждой своя задача
программистам: параметры, потери и пропуски кадров, декодирование. Потери здесь считаются по TCP:
он сам ничего не теряет, поэтому разрыв номеров значит, что пакеты выбросила камера.
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

from qa_tool.checks.stream.common import (
    CODEC_FORMATS,
    MAX_PROFILES,
    OPEN_RESERVE_S,
    RTSP_UNAUTHORIZED,
    DecoderFailed,
    Sampler,
    ToolMissing,
    add_onvif_evidence,
    check_timeout_s,
    client_for,
    decode,
    limit,
    read_profiles,
    refused_password,
    remember_refusal,
    sample_seconds,
    start_video,
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
from qa_tool.drivers.camera.depacket import jpeg_size
from qa_tool.drivers.camera.onvif_generic import EncoderSettings, MediaProfile
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, RtspError, Track, WatchEnd
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

CHECK_ID = "STREAM-PARAMS-1"
ENOUGH_OF_SAMPLE = 0.8  # меньше — поток прервался, а не «немного короче»
JPEG_NAMES = {"JPEG", "MJPEG"}


def _name(profile: MediaProfile) -> str:
    return profile.name or profile.token


def _size_of(encoder: EncoderSettings) -> str:
    return f"{encoder.width}×{encoder.height}"


def bitrate_problem(encoder: EncoderSettings, kbps: float, pct: float) -> str:
    """Что не так с битрейтом; пусто — в допуске.

    При постоянном битрейте `BitrateLimit` — цель, при переменном — потолок: на тихой сцене поток
    законно намного меньше. Если камера вид не сообщила, сверяем только потолок, чтобы не завести
    ложную задачу.
    """
    if not encoder.kbps:
        return ""
    if kbps > encoder.kbps * (1 + pct / 100):
        return f"битрейт {kbps:g} кбит/с выше заявленных {encoder.kbps:g}"
    if encoder.constant and kbps < encoder.kbps * (1 - pct / 100):
        return f"битрейт {kbps:g} кбит/с ниже постоянных {encoder.kbps:g}"
    return ""


def _size_expected(encoder: EncoderSettings, encoding: str) -> tuple[int, int]:
    """Размер, который должен прийти: RTP JPEG хранит его блоками по 8 пикселей (RFC 2435)."""
    if encoding.upper() in JPEG_NAMES:
        return -(-encoder.width // 8) * 8, -(-(encoder.height or 0) // 8) * 8
    return encoder.width, encoder.height


def param_problems(
    ctx: CheckContext,
    encoder: EncoderSettings,
    stats: dict,
    encoding: str,
    size: tuple[int, int] | None,
) -> list[str]:
    """Расхождения заявленного и измеренного.

    Args:
        ctx: контекст проверки, из него пороги.
        encoder: что заявляет камера.
        stats: итог `StreamMeter.stats`.
        encoding: кодек из SDP.
        size: измеренное разрешение; None — измерить не удалось.

    Returns:
        Тексты расхождений для протокола; пусто — всё в допуске.
    """
    problems = []
    declared = encoder.encoding.replace("MJPEG", "JPEG")
    if encoding.upper().replace("HEVC", "H265") != declared:
        problems.append(f"кодек {encoding}, заявлен {encoder.encoding}")
    if size and encoder.width and size != _size_expected(encoder, encoding):
        problems.append(f"разрешение {size[0]}×{size[1]}, заявлено {_size_of(encoder)}")
    fps_pct = limit(ctx, "stream.fps_pct").value
    if encoder.fps and abs(stats["fps"] - encoder.fps) > encoder.fps * fps_pct / 100:
        problems.append(f"частота {stats['fps']:g} кадр/с, заявлено {encoder.fps:g}")
    arrival = stats["arrival_fps"]
    if arrival and abs(arrival - stats["fps"]) > stats["fps"] * fps_pct / 100:
        problems.append(
            f"кадры приходят с частотой {arrival:g} кадр/с, а метки времени "
            f"показывают {stats['fps']:g}: частота и битрейт по меткам неверны"
        )
    bitrate = bitrate_problem(encoder, stats["kbps"], limit(ctx, "stream.bitrate_pct").value)
    if bitrate:
        problems.append(bitrate)
    gop_frames = limit(ctx, "stream.gop_frames").value
    if encoder.gop and stats["gop"] and abs(stats["gop"] - encoder.gop) > gop_frames:
        problems.append(f"GOP {stats['gop']} кадров, заявлено {encoder.gop}")
    return problems


def measured_text(stats: dict, encoding: str, size: tuple[int, int] | None) -> str:
    """Измеренное для столбца «факт»."""
    shown = f"{size[0]}×{size[1]}, " if size else ""
    gop = f", GOP {stats['gop']}" if stats["gop"] else ""
    return f"{encoding}, {shown}{stats['fps']:g} кадр/с, {stats['kbps']:g} кбит/с{gop}"


def declared_text(encoder: EncoderSettings) -> str:
    """Заявленное для столбца «ожидание»."""
    parts = [encoder.encoding]
    if encoder.width:
        parts.append(_size_of(encoder))
    if encoder.fps:
        parts.append(f"{encoder.fps:g} кадр/с")
    if encoder.kbps:
        kind = {True: "постоянный ", False: "до ", None: "до "}[encoder.constant]
        parts.append(f"{kind}{encoder.kbps:g} кбит/с")
    if encoder.gop:
        parts.append(f"GOP {encoder.gop}")
    return ", ".join(parts)


class ProfileRun:
    """Проверка одного профиля: выборка и её строки протокола.

    Args:
        ctx: контекст проверки.
        profile: медиапрофиль.
        number: номер профиля с 1, входит в ID строк и имена файлов.
        folder: временная папка выборки.
    """

    def __init__(self, ctx: CheckContext, profile: MediaProfile, number: int, folder: Path):
        self.ctx = ctx
        self.profile = profile
        self.number = number
        self.folder = folder
        self.track: Track | None = None
        self.meter: StreamMeter | None = None
        self.sampler: Sampler | None = None
        self.jpeg: tuple[int, int] | None = None
        self.sdp = ""
        self.channel = 0
        self.decoder_problem = ""
        self.exchanges: list[str] = []

    def row(self, kind: str, title: str, result: CheckResult, severity: Severity) -> ChildResult:
        """Строка протокола этого профиля."""
        return ChildResult(
            f"STREAM-{kind}-{self.number}",
            f"Поток «{_name(self.profile)}»: {title}",
            result,
            f"Выборка потока профиля {self.profile.token} по RTSP поверх TCP.",
            severity,
        )

    def _on_packet(self, packet: RtpPacket) -> None:
        if packet.channel != self.channel:
            return
        self.meter.add(packet)
        if self.sampler:
            self.sampler.add(packet)
        elif self.jpeg is None:
            self.jpeg = jpeg_size(packet.payload)

    def _read(self, client: RtspClient, seconds: float) -> WatchEnd:
        with client:
            self.sdp, self.track = start_video(client)
            self.channel = client.rtp_channel
            self.meter = StreamMeter(self.track.encoding, self.track.clock_rate)
            if self.track.encoding.upper() in CODEC_FORMATS:
                self.sampler = Sampler(self.folder / f"sample-{self.number}", self.track)
            return client.watch(self._on_packet, seconds, self.ctx.stop_requested)

    def run(self, seconds: float) -> list[ChildResult]:
        """Снять выборку и вернуть строки профиля."""
        problem = self.profile.problem or (
            "" if self.profile.uri.startswith("rtsp://") else "камера не сообщила адрес RTSP"
        )
        if problem:
            return [self._failed_open(problem)]
        client = client_for(self.ctx, self.profile)
        try:
            end = self._read(client, seconds)
        except RtspError as e:
            self.exchanges = client.exchanges
            if e.status == RTSP_UNAUTHORIZED:
                result = CheckResult.stand_error(remember_refusal(self.ctx, e))
                return [self.row("PARAMS", "параметры", result, Severity.MAJOR)]
            return [self._failed_open(f"поток не открывается: {e}")]
        except (OSError, ValueError) as e:
            self.exchanges = client.exchanges
            return [self._failed_open(f"поток не открывается: {e}")]
        finally:
            has_sample = self.sampler.close() if self.sampler else False
        self.exchanges = client.exchanges
        stats = self.meter.stats()
        cut = self._cut_short(end, stats, seconds)
        if end is WatchEnd.STOPPED:
            result = CheckResult.skipped("прогон остановлен")
            return [self.row("PARAMS", "параметры", result, Severity.MAJOR)]
        errors, info = self._decoded(has_sample)
        size = self._size(info)
        return [
            self._params_row(stats, size, cut),
            self._loss_row(stats),
            self._decode_row(has_sample, errors, info),
        ]

    def _cut_short(self, end: WatchEnd, stats: dict, seconds: float) -> str:
        if end in (WatchEnd.SILENT, WatchEnd.BROKEN):
            return f"поток прервался: {end.value}, успел {stats['seconds']:g} с"
        if stats["seconds"] < seconds * ENOUGH_OF_SAMPLE:
            return f"видео шло {stats['seconds']:g} с из {seconds:g}"
        return ""

    def _evidence(self, stats: dict) -> list[Evidence]:
        text = json.dumps(stats, ensure_ascii=False, indent=1)
        return [
            Evidence(f"stream-{self.number}.json", text),
            Evidence(f"sdp-{self.number}.txt", self.sdp),
            Evidence(f"rtsp-{self.number}.txt", "\n".join(self.exchanges)),
        ]

    def _failed_open(self, reason: str) -> ChildResult:
        evidence = [Evidence(f"rtsp-{self.number}.txt", "\n".join(self.exchanges))]
        result = CheckResult(
            Status.FAILED, reason=reason, expected="поток открывается", evidence=evidence
        )
        return self.row("PARAMS", "параметры", result, Severity.BLOCKER)

    def _decoded(self, has_sample: bool) -> tuple[list[str] | None, dict]:
        if not has_sample:
            return [], {}
        try:
            return decode(self.folder / f"sample-{self.number}", self.track.encoding)
        except ToolMissing:
            return None, {}
        except DecoderFailed as e:
            self.decoder_problem = str(e)
        except subprocess.TimeoutExpired:
            self.decoder_problem = "декодер не уложился в срок: компьютер стенда перегружен"
        return [], {}

    def _size(self, info: dict) -> tuple[int, int] | None:
        if info.get("width") and info.get("height"):
            return int(info["width"]), int(info["height"])
        return self.jpeg

    def _params_row(self, stats: dict, size: tuple[int, int] | None, cut: str) -> ChildResult:
        encoder = self.profile.encoder
        actual = measured_text(stats, self.track.encoding, size)
        evidence = self._evidence(stats)
        title = "параметры"
        if cut:
            result = CheckResult(
                Status.FAILED,
                reason=cut,
                expected="поток идёт всю выборку",
                actual=actual,
                evidence=evidence,
            )
        elif encoder is None:
            result = CheckResult(
                Status.SKIPPED,
                reason="камера не сообщает настройки кодировщика: сверять не с чем",
                actual=actual,
                evidence=evidence,
            )
        else:
            problems = param_problems(self.ctx, encoder, stats, self.track.encoding, size)
            unmeasured = [
                name
                for name, missed in (
                    ("разрешение", encoder.width and not size),
                    ("GOP", encoder.gop and not stats["gop"]),
                )
                if missed
            ]
            note = f"; не измерено: {', '.join(unmeasured)}" if unmeasured else ""
            result = CheckResult(
                Status.FAILED if problems else Status.PASSED,
                reason="; ".join(problems),
                expected=declared_text(encoder),
                actual=actual + note,
                evidence=evidence,
            )
        return self.row("PARAMS", title, result, Severity.MAJOR)

    def _loss_row(self, stats: dict) -> ChildResult:
        if not stats["packets"]:
            result = CheckResult.skipped("пакетов не было: см. строку параметров")
            return self.row("LOSS", "потери и пропуски кадров", result, Severity.MAJOR)
        drop = limit(self.ctx, "stream.drop_pct")
        problems = []
        if stats["lost"]:
            problems.append(
                f"камера потеряла {stats['lost']} пакетов ({stats['loss_pct']:g} %) по TCP"
            )
        if stats["drop_pct"] > drop.value:
            problems.append(
                f"пропущено {stats['dropped']} кадров ({stats['drop_pct']:g} %), "
                f"самый длинный промежуток {stats['longest_gap_s']:g} с"
            )
        result = CheckResult(
            Status.FAILED if problems else Status.PASSED,
            reason="; ".join(problems),
            expected=f"потерь по TCP нет, пропусков кадров ≤ {drop.value:g} {drop.unit}",
            actual=f"потеряно {stats['lost']} из {stats['packets'] + stats['lost']}, "
            f"пропущено кадров {stats['dropped']} из {stats['frames'] + stats['dropped']}",
            evidence=[Evidence(f"stream-{self.number}.json", json.dumps(stats, indent=1))],
            threshold=drop,
        )
        return self.row("LOSS", "потери и пропуски кадров", result, Severity.MAJOR)

    def _decode_row(self, has_sample: bool, errors: list[str] | None, info: dict) -> ChildResult:
        expected = "выборка декодируется без ошибок"
        if self.sampler is None:
            result = CheckResult.skipped(f"кодек {self.track.encoding}: декодер не применяется")
        elif errors is None:
            result = CheckResult.stand_error("нет ffmpeg или ffprobe: декодировать нечем")
        elif self.decoder_problem:
            result = CheckResult.stand_error(self.decoder_problem)
        elif not has_sample:
            result = CheckResult(
                Status.FAILED, reason="в выборке нет ключевого кадра", expected=expected
            )
        elif errors:
            log = Evidence(f"decoder-{self.number}.txt", "\n".join(errors))
            result = CheckResult(
                Status.FAILED,
                reason=f"ошибки декодера: {errors[0]}",
                expected=expected,
                evidence=[log],
            )
        else:
            log = Evidence(f"decoder-{self.number}.txt", json.dumps(info))
            result = CheckResult(
                Status.PASSED, expected=expected, actual="ошибок нет", evidence=[log]
            )
        return self.row("DECODE", "декодирование", result, Severity.BLOCKER)


def _plain_row(profile: MediaProfile, number: int, result: CheckResult) -> ChildResult:
    return ChildResult(
        f"STREAM-PARAMS-{number}",
        f"Поток «{_name(profile)}»: параметры",
        result,
    )


@REGISTRY.check(
    CHECK_ID,
    module="stream",
    title="Параметры потоков",
    description="Каждый медиапрофиль читается по RTSP поверх TCP. Сверяются кодек, разрешение, "
    "частота кадров, битрейт и GOP с настройками камеры, считаются потери и пропуски кадров, "
    "выборка декодируется. Настройки камеры не меняются.",
    phase=Phase.NETWORK,
    order=800,
    timeout_s=check_timeout_s(),
    group="Поток",
    help_id="stream",
)
def stream_params(ctx: CheckContext) -> CheckResult:
    """Проверить все медиапрофили камеры по очереди: разом они мешали бы измерять друг друга."""
    try:
        profiles, evidence = read_profiles(ctx)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(Status.FAILED, reason=f"ONVIF: {e}", expected="профили читаются")
    seconds = sample_seconds(ctx)
    deadline = time.monotonic() + MAX_PROFILES * (seconds + OPEN_RESERVE_S)
    rows: list[ChildResult] = []
    with tempfile.TemporaryDirectory(prefix="qa-stream-") as folder:
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
                rows.append(_plain_row(profile, number, CheckResult.skipped(reason)))
                continue
            try:
                rows += ProfileRun(ctx, profile, number, Path(folder)).run(seconds)
            except Exception as e:  # данные одной камеры не должны стоить строк остальных профилей
                result = CheckResult.stand_error(f"сбой проверки профиля: {type(e).__name__}")
                rows.append(_plain_row(profile, number, result))
    add_onvif_evidence(rows, evidence)
    return CheckResult(
        Status.PASSED,
        actual=f"потоков: {len(profiles)}",
        evidence=evidence,
        children=rows,
    )
