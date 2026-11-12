"""Кадр с камеры для проверки картинки: своя RTSP-сессия на снимок, декодер, стабильность.

На каждый снимок — новая сессия RTSP (`RtspClient`, табу AD-7: только методы на чтение): камера
после записи настройки может перезапустить кодировщик, и старая сессия показала бы прежний кадр.
Выборка начинается с ключевого кадра (`Sampler`), ffmpeg получает только временный файл — адреса
и пароля в его аргументах нет. Временные файлы — в папке `qa-image-*`, она удаляется сразу, а
оставшиеся от снятого процесса — при старте следующего прогона (`core/tempfiles.py`).

Кадр «стабилен», когда средняя яркость последних `settle_frames` кадров меняется не больше
`settle_luma`: автоэкспозиция и баланс белого камеры успокаиваются не сразу после записи.
"""

import json
import shutil
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from qa_tool.checks.onvif.metadata import to_stand_address
from qa_tool.checks.rtsp_refusal import is_refusal, refused_password, remember_refusal
from qa_tool.checks.stream.common import (
    CODEC_FORMATS,
    DECODE_TIMEOUT_S,
    DecoderFailed,
    Sampler,
    ToolMissing,
    start_video,
)
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Threshold
from qa_tool.core.tempfiles import IMAGE_PREFIX
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, RtspError
from qa_tool.tools import tool_path

START = "стартовое значение: уточнить по эталонным камерам"
DEFAULTS = {
    "image.keyframe_wait_s": Threshold(12.0, "с", START),  # Smart GOP — ключевой кадр до 10 с
    "image.clip_s": Threshold(2.0, "с", START),
    "image.settle_frames": Threshold(5.0, "кадр", START),
    "image.settle_luma": Threshold(1.0, "уровень", START),
    "image.settle_max_s": Threshold(10.0, "с", START),
}
SAMPLE_BYTES = 32 << 20  # выборка на снимок: 2 с даже 4K-потока много меньше
MAX_FRAMES = 300  # кадров на снимок; больше — камера шлёт что-то не то
WORK_WIDTH = 1280  # рабочий кадр — весь, но не шире: величины считаются по нему
FRAGMENT = (1280, 720)  # центр кадра в родном разрешении — для резкости и шума
MAX_WORK_HEIGHT = 2160  # после сжатия до 1280 по ширине выше бывает только у мусора
RING = 8  # кадров для временного шума (шумоподавление)
THUMB_WIDTH = 480
RTSP_TIMEOUT_S = 10.0


class StreamRefused(RuntimeError):
    """Камера не приняла пароль на видео — «ошибка стенда», повторов нет."""


class StreamUnavailable(RuntimeError):
    """Поток не открылся или не дал ключевого кадра — «ошибка стенда»."""


class FrameTooLarge(RuntimeError):
    """Кадр больше разумного — разбирать его стенд не станет."""


@dataclass(frozen=True)
class Timings:
    """Сроки снимка из `thresholds.yaml` (ключи `image.*`), иначе стартовые."""

    keyframe_wait_s: float = 12.0
    clip_s: float = 2.0
    settle_frames: int = 5
    settle_luma: float = 1.0
    settle_max_s: float = 10.0

    @classmethod
    def of(cls, ctx: CheckContext) -> "Timings":
        """Сроки для камеры из порогов стенда."""

        def value(key: str) -> float:
            return ctx.thresholds.get(key, DEFAULTS[key]).value

        return cls(
            value("image.keyframe_wait_s"),
            value("image.clip_s"),
            max(2, int(value("image.settle_frames"))),
            value("image.settle_luma"),
            value("image.settle_max_s"),
        )


@dataclass
class Capture:
    """Снимок: рабочий кадр и фрагмент (RGB, uint8), последние кадры, превью."""

    work: np.ndarray
    fragment: np.ndarray
    recent: list[np.ndarray]  # фрагменты последних кадров, до `RING` штук
    width: int  # родное разрешение потока
    height: int
    codec: str
    settle_s: float  # сколько ждали стабильного кадра
    stable: bool
    lumas: list[float] = field(default_factory=list)  # средняя Y рабочих кадров последней выборки
    clip: bytes | None = None  # выборка потока — только если просили
    preview_jpeg: bytes = b""
    thumb_jpeg: bytes = b""


def luma(rgb: np.ndarray) -> np.ndarray:
    """Яркость Y по BT.709, 0–255."""
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def read_ppm(stream, limit: int) -> np.ndarray | None:
    """Один кадр PPM (P6) из потока ffmpeg; None — поток кончился.

    Raises:
        FrameTooLarge: размер кадра больше `limit` байт.
        DecoderFailed: заголовок не PPM.
    """
    tokens: list[bytes] = []
    while len(tokens) < 4:
        token = b""
        while True:
            char = stream.read(1)
            if not char:
                if tokens or token:
                    raise DecoderFailed("кадр ffmpeg оборван")
                return None
            if char.isspace():
                if token:
                    break
                continue
            token += char
            if len(token) > 16:
                raise DecoderFailed("заголовок кадра ffmpeg не PPM")
        tokens.append(token)
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise DecoderFailed("заголовок кадра ffmpeg не PPM")
    width, height = int(tokens[1]), int(tokens[2])
    if width * height * 3 > limit:
        raise FrameTooLarge(f"кадр {width}×{height} больше предела стенда")
    data = stream.read(width * height * 3)
    if len(data) < width * height * 3:
        raise DecoderFailed("кадр ffmpeg оборван")
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)


class RtspFrameSource:
    """Снимки потока одного профиля камеры.

    Args:
        ctx: контекст проверки (камера, пароль, пороги).
        uri: адрес RTSP из GetStreamUri; хост заменяется адресом камеры из стенда.
        timings: сроки снимка; None — пороги `image.*` стенда. Кодекам нужна одна выборка
            (`settle_max_s=0`) длиной в несколько групп кадров.
        sample_bytes: наибольший размер выборки, байт.
        prefix: префикс временной папки из `core/tempfiles.py` — по нему её уберёт следующий
            прогон, если процесс снимут.
    """

    def __init__(
        self,
        ctx: CheckContext,
        uri: str,
        timings: Timings | None = None,
        sample_bytes: int = SAMPLE_BYTES,
        prefix: str = IMAGE_PREFIX,
    ):
        self.ctx = ctx
        self.url = to_stand_address(uri, ctx.camera.address, ctx.camera.rtsp_port)
        self.timings = timings or Timings.of(ctx)
        self.sample_bytes = sample_bytes
        self.prefix = prefix
        self.exchanges: list[str] = []  # обмен RTSP без секретов — в доказательства

    def capture(
        self, keep_clip: bool = False, on_packet: Callable[[RtpPacket], None] | None = None
    ) -> Capture:
        """Снять стабильный кадр: выборки подряд, пока кадр не успокоится или не выйдет срок.

        Args:
            keep_clip: сохранить выборку потока последней попытки (для «не прошла»).
            on_packet: получит пакеты видео той выборки, что дала кадр, от ключевого кадра —
                битрейт меряется по тем же данным, что и качество. Отдаются после декодирования,
                поэтому ранние выборки, пока кадр не успокоился, сюда не попадают.

        Raises:
            StreamRefused, StreamUnavailable, FrameTooLarge, ToolMissing, DecoderFailed.
        """
        ffmpeg, ffprobe = tool_path("ffmpeg"), tool_path("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ToolMissing("нет ffmpeg или ffprobe")
        refused = refused_password(self.ctx)
        if refused:
            raise StreamRefused(refused)
        folder = Path(tempfile.mkdtemp(prefix=f"{self.prefix}{self.ctx.camera.slot}-"))
        started = time.monotonic()
        packets: list[RtpPacket] = []
        try:
            while True:
                packets = []
                sample, codec = self._sample(folder, packets if on_packet else None)
                capture = self._decode(ffmpeg, ffprobe, sample, codec)
                capture.settle_s = round(time.monotonic() - started, 1)
                if capture.stable or capture.settle_s >= self.timings.settle_max_s:
                    break
            if on_packet is not None:
                for packet in packets:
                    on_packet(packet)
            capture.clip = sample.read_bytes() if keep_clip else None
            capture.preview_jpeg, capture.thumb_jpeg = self._jpeg(ffmpeg, folder, capture.work)
            return capture
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _sample(self, folder: Path, packets: list[RtpPacket] | None) -> tuple[Path, str]:
        """Выборка потока с ключевого кадра длиной `clip_s` в файл; `packets` — копить пакеты
        выборки от ключевого кадра."""
        camera = self.ctx.camera
        client = RtspClient(self.url, camera.username, self.ctx.password, timeout=RTSP_TIMEOUT_S)
        path = folder / "sample"
        try:
            try:
                client.connect()
                _sdp, track = start_video(client)
            except RtspError as e:
                if is_refusal(e):
                    raise StreamRefused(remember_refusal(self.ctx, e)) from e
                raise StreamUnavailable(f"поток не открывается: {e}") from e
            except (OSError, ValueError) as e:
                raise StreamUnavailable(f"поток не открывается: {e}") from e
            if track.encoding.upper() not in CODEC_FORMATS:
                raise StreamUnavailable(f"кодек {track.encoding} стенд не декодирует")
            sampler = Sampler(path, track, limit=self.sample_bytes)
            span = int(self.timings.clip_s * track.clock_rate)
            first: list[int] = []
            frames: set[int] = set()

            def enough(packet: RtpPacket) -> bool:
                if packet.channel != 0:
                    return False
                sampler.add(packet)
                if sampler.started and not first:
                    first.append(packet.timestamp)
                if first:
                    if packets is not None:
                        packets.append(packet)
                    frames.add(packet.timestamp)
                    passed = (packet.timestamp - first[0]) % (1 << 32)
                    return passed >= span or sampler.full or len(frames) > MAX_FRAMES
                return False

            client.read_packets(self.timings.keyframe_wait_s + self.timings.clip_s, until=enough)
            has_frames = sampler.close()
            if not first or not has_frames:
                raise StreamUnavailable(
                    f"нет ключевого кадра за {self.timings.keyframe_wait_s:g} с"
                )
            return path, track.encoding
        finally:
            self.exchanges.extend(client.exchanges)
            client.close()

    def _decode(self, ffmpeg: str, ffprobe: str, sample: Path, codec: str) -> Capture:
        """Рабочий кадр и фрагмент, склеенные по вертикали одним вызовом ffmpeg."""
        source = ["-f", CODEC_FORMATS[codec.upper()], "-i", str(sample)]
        width, height = self._size(ffprobe, source)
        if width <= 0 or height <= 0:
            raise DecoderFailed("ffprobe не назвал размер кадра")
        work_w = min(WORK_WIDTH, width)
        work_h = max(2, round(height * work_w / width / 2) * 2)
        frag_h = min(FRAGMENT[1], height)
        if work_h > MAX_WORK_HEIGHT:
            raise FrameTooLarge(f"кадр {width}×{height} больше предела стенда")
        graph = (
            f"[0:v]split=2[a][b];[a]scale={work_w}:{work_h}:flags=area[w];"
            f"[b]crop={work_w}:{frag_h}[f];[w][f]vstack=2,format=rgb24"
        )
        command = [ffmpeg, "-hide_banner", "-v", "error", *source, "-filter_complex", graph]
        command += ["-frames:v", str(MAX_FRAMES), "-f", "image2pipe", "-c:v", "ppm", "-"]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ring: deque[np.ndarray] = deque(maxlen=RING)
        lumas: list[float] = []
        deadline = time.monotonic() + DECODE_TIMEOUT_S
        try:
            while time.monotonic() < deadline:
                frame = read_ppm(process.stdout, work_w * (work_h + frag_h) * 3)
                if frame is None:
                    break
                if frame.shape[0] != work_h + frag_h:
                    raise DecoderFailed("ffmpeg отдал кадр не того размера")
                ring.append(frame)
                lumas.append(float(luma(frame[:work_h]).mean()))
        finally:
            process.kill()
            process.wait()
        if not ring:
            raise DecoderFailed("ffmpeg не отдал ни одного кадра")
        last = ring[-1]
        window = lumas[-self.timings.settle_frames :]
        stable = (
            len(window) >= self.timings.settle_frames
            and max(window) - min(window) <= self.timings.settle_luma
        )
        return Capture(
            work=last[:work_h].copy(),
            fragment=last[work_h:].copy(),
            recent=[f[work_h:].copy() for f in ring],
            width=width,
            height=height,
            codec=codec.lower(),
            settle_s=0.0,
            stable=stable,
            lumas=lumas,
        )

    @staticmethod
    def _size(ffprobe: str, source: list[str]) -> tuple[int, int]:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0"]
            + ["-show_entries", "stream=width,height", "-of", "json", *source[:2], source[3]],
            capture_output=True,
            timeout=DECODE_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            stream = (json.loads(done.stdout or b"{}").get("streams") or [{}])[0]
            return int(stream.get("width", 0)), int(stream.get("height", 0))
        except (ValueError, TypeError, AttributeError):
            return 0, 0

    @staticmethod
    def _jpeg(ffmpeg: str, folder: Path, work: np.ndarray) -> tuple[bytes, bytes]:
        """Превью (≤ 1280 px, качество ≈ 85) и миниатюра 480 px вторым вызовом ffmpeg."""
        height, width = work.shape[:2]
        header = f"P6\n{width} {height}\n255\n".encode()
        preview, thumb = folder / "preview.jpg", folder / "thumb.jpg"
        command = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "image2pipe", "-c:v", "ppm"]
        command += ["-i", "-", "-filter_complex", f"[0:v]split[p][t];[t]scale={THUMB_WIDTH}:-2[s]"]
        command += ["-map", "[p]", "-q:v", "3", "-frames:v", "1", str(preview)]
        command += ["-map", "[s]", "-q:v", "5", "-frames:v", "1", str(thumb)]
        done = subprocess.run(
            command,
            input=header + work.tobytes(),
            capture_output=True,
            timeout=DECODE_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode or not preview.exists() or not thumb.exists():
            raise DecoderFailed(f"ffmpeg не сохранил превью (код {done.returncode})")
        return preview.read_bytes(), thumb.read_bytes()
