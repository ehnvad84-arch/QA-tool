"""Общее для проверок потока: пороги, сроки, чтение профилей, выборка для декодера."""

import json
import os
import subprocess
from pathlib import Path

from qa_tool.checks.onvif.metadata import to_stand_address
from qa_tool.checks.onvif.replay import is_keyframe, soap_evidence
from qa_tool.core.checks import CheckContext, ChildResult, Evidence
from qa_tool.core.models import Threshold
from qa_tool.drivers.camera.depacket import Depacketizer, parameter_sets
from qa_tool.drivers.camera.onvif_generic import MediaProfile, media_profiles, service_urls
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, Track
from qa_tool.drivers.camera.soap import SoapClient
from qa_tool.tools import tool_path

START = "стартовое значение: уточнить по результатам стенда"
DEFAULTS = {
    "stream.sample_s": Threshold(30.0, "с", START),
    "stream.bitrate_pct": Threshold(20.0, "%", START),
    "stream.fps_pct": Threshold(10.0, "%", START),
    "stream.gop_frames": Threshold(1.0, "кадр", START),
    "stream.drop_pct": Threshold(0.1, "%", START),
    "stream.udp_loss_pct": Threshold(0.1, "%", START),
    "stream.jitter_ms": Threshold(30.0, "мс", START),
    "stream.delay_growth_s": Threshold(1.0, "с", START),
    # часы камеры уходят постоянно и за сутки дали бы секунды роста без всякой очереди: у ухода
    # 50 ppm это 0,18 с/ч, у меток 30 кадр/с на настоящих 29,97 — 3,6 с/ч; очередь растёт быстрее
    "stream.delay_rate_s_h": Threshold(5.0, "с/ч", START),
}
MAX_PROFILES = 8  # у камер бывает 2–4; предел держит проверку в её сроке
SAMPLE_CEILING_S = 60.0  # больше порог не поднимет: срок проверки считается от этого значения
# на профиль: открыть поток, закрыть, декодировать; два запуска декодера по DECODE_TIMEOUT_S
# в него не помещаются, но и бывают только при сбое: запас покрывает RESERVE_S
OPEN_RESERVE_S = 25.0
RESERVE_S = 60.0  # на чтение профилей и закрытие: срок планировщика не должен наступить раньше
DECODE_TIMEOUT_S = 20.0
SAMPLE_LIMIT = 64 << 20  # 30 с потока 16 Мбит/с; больше декодеру не нужно
CODEC_FORMATS = {"H264": "h264", "H265": "hevc", "HEVC": "hevc"}
DECODER_LINES_LIMIT = 20  # строк ошибок декодера в доказательствах
ENOUGH_OF_SAMPLE = 0.8  # меньше — поток прервался, а не «немного короче»


class ToolMissing(RuntimeError):
    """Нет ffmpeg или ffprobe: вина стенда, а не камеры."""


class DecoderFailed(RuntimeError):
    """ffmpeg упал, не сказав об ошибках в потоке: вина стенда, а не камеры."""


def limit(ctx: CheckContext, key: str) -> Threshold:
    """Порог из настроек стенда, иначе стартовый."""
    return ctx.thresholds.get(key, DEFAULTS[key])


def sample_seconds(ctx: CheckContext) -> float:
    """Длина выборки на профиль, с.

    `QA_TOOL_STREAM_SAMPLE_S` укорачивает её в тестах: рабочие процессы запускаются заново и
    параметров из тестов не видят.
    """
    override = os.environ.get("QA_TOOL_STREAM_SAMPLE_S")
    value = float(override) if override else limit(ctx, "stream.sample_s").value
    return min(value, SAMPLE_CEILING_S)


def check_timeout_s() -> float:
    """Срок проверки по профилям для планировщика: снятие процесса потеряло бы все строки."""
    return MAX_PROFILES * (SAMPLE_CEILING_S + OPEN_RESERVE_S) + RESERVE_S


def read_profiles(ctx: CheckContext) -> tuple[list[MediaProfile], list[Evidence]]:
    """Медиапрофили камеры и обмен с ней для доказательств.

    Raises:
        CameraUnreachable, OnvifFault: профилей прочитать не удалось.
    """
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    profiles = media_profiles(soap, service_urls(soap, ctx.camera))
    return profiles, soap_evidence(soap)


def client_for(ctx: CheckContext, profile: MediaProfile, timeout: float = 10.0) -> RtspClient:
    """Клиент RTSP на адрес камеры со стенда: хост из ответа камеры недоверенный (NAT)."""
    url = to_stand_address(profile.uri, ctx.camera.address, ctx.camera.rtsp_port)
    return RtspClient(url, ctx.camera.username, ctx.password, timeout=timeout)


def add_onvif_evidence(rows: list[ChildResult], evidence: list[Evidence]) -> None:
    """Положить обмен ONVIF в первую строку: родитель с дочерними строками в базу не пишется."""
    if rows:
        rows[0].result.evidence = [*rows[0].result.evidence, *evidence]


def start_video(client: RtspClient) -> tuple[str, Track]:
    """Запустить видеодорожку на канале 0.

    Returns:
        SDP и дорожка.

    Raises:
        OSError, RtspError: поток не открылся; ValueError — в SDP нет видео.
    """
    sdp, tracks = client.describe()
    track = next((t for t in tracks if t.media == "video"), None)
    if track is None:
        raise ValueError("в описании потока (SDP) нет видеодорожки")
    client.setup(track, channel=0)
    client.play()
    return sdp, track


class Sampler:
    """Выборка для декодера: с первого ключевого кадра и только целыми кадрами.

    Начало посреди группы кадров и оборванный последний кадр декодер считает ошибками, а камера
    в этом не виновата.

    Args:
        path: файл выборки.
        track: видеодорожка.
        limit: наибольший размер выборки, байт; кадр сверх него не пишется и выборка кончается.
    """

    def __init__(self, path: Path, track: Track, limit: int = SAMPLE_LIMIT):
        self.path = path
        self.limit = limit
        self.encoding = track.encoding
        self._depacketizer = Depacketizer(track.encoding, parameter_sets(track.fmtp))
        self._file = path.open("wb")
        self._file.write(self._depacketizer.start())
        self._frame = bytearray()
        self._timestamp: int | None = None
        self._frame_key = False
        self._started = False
        self._written = 0
        self._full = False

    def add(self, packet: RtpPacket) -> None:
        """Учесть пакет видео."""
        if self._full:
            return
        if packet.timestamp != self._timestamp:
            self._flush()
            self._timestamp = packet.timestamp
        self._frame += self._depacketizer.push(packet.payload)
        self._frame_key = self._frame_key or is_keyframe(self.encoding, packet.payload)
        # выборка кончается целиком: кадры после пропущенного декодер счёл бы ошибками камеры
        self._full = self._written + len(self._frame) > self.limit

    def _flush(self) -> None:
        self._started = self._started or self._frame_key
        if self._started:
            self._file.write(self._frame)
            self._written += len(self._frame)
        self._frame.clear()
        self._frame_key = False

    @property
    def started(self) -> bool:
        """Ключевой кадр уже был: дальше в файл идут целые кадры."""
        return self._started or self._frame_key

    @property
    def full(self) -> bool:
        """Предел байтов достигнут: дальше пакеты не пишутся."""
        return self._full

    def close(self) -> bool:
        """Закрыть файл; True — в нём есть хотя бы один целый кадр от ключевого."""
        self._file.close()
        return self._written > 0


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=DECODE_TIMEOUT_S,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 0 — не Windows
    )


def decode(path: Path, encoding: str) -> tuple[list[str], dict]:
    """Декодировать выборку из файла; адреса камеры и пароля в аргументах нет.

    Args:
        path: файл выборки.
        encoding: кодек из SDP, один из `CODEC_FORMATS`.

    Returns:
        Строки ошибок декодера и данные потока от ffprobe (`width`, `height`, `codec_name`).

    Raises:
        ToolMissing: нет ffmpeg или ffprobe.
        DecoderFailed: ffmpeg упал и не назвал ошибок потока.
        subprocess.TimeoutExpired: декодер не уложился в срок.
    """
    ffmpeg, ffprobe = tool_path("ffmpeg"), tool_path("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolMissing("нет ffmpeg или ffprobe")
    source = ["-f", CODEC_FORMATS[encoding.upper()], "-i", str(path)]
    done = _run([ffmpeg, "-hide_banner", "-v", "error", *source, "-f", "null", "-"])
    errors = [line for line in done.stderr.splitlines() if line.strip()][:DECODER_LINES_LIMIT]
    if done.returncode and not errors:
        raise DecoderFailed(f"ffmpeg завершился с кодом {done.returncode}")
    probe = _run(
        [ffprobe, "-v", "error", "-select_streams", "v:0"]
        + ["-show_entries", "stream=width,height,codec_name", "-of", "json"]
        + source[:2]
        + [str(path)]
    )
    try:
        streams = json.loads(probe.stdout or "{}").get("streams") or [{}]
    except json.JSONDecodeError:
        streams = [{}]
    return errors, streams[0]
