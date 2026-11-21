"""Превью потока выбранной камеры на экране «Прогон».

Кадр снимает тот же `RtspFrameSource`, что и проверки «Картинки»: ONVIF — через `http.py`, RTSP —
только методы на чтение (табу AD-7). Адрес потока узнаётся по ONVIF один раз на камеру, дальше —
снимок раз в `PERIOD_S` в фоновой нити окна. Берётся последний медиапрофиль: у камер это обычно
второй, лёгкий поток, и превью меньше нагружает камеру.

Отказ в пароле (ONVIF или RTSP) останавливает превью этой камеры, пока пароль в таблице не
поменяют: камеры считают попытки входа и блокируют. Во время прогона и когда экран не виден
превью стоит: лишнее подключение во время проверок потока могло бы сорвать их (камеры ограничивают
число зрителей), а невидимое превью только тратит сеть.
"""

import hashlib
import threading

from PySide6.QtCore import QObject, Signal

from qa_tool.checks.image.frames import RtspFrameSource, StreamRefused, Timings
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, ConfigError, Place
from qa_tool.core.secrets import get_password
from qa_tool.core.tempfiles import IMAGE_PREFIX
from qa_tool.drivers.camera.onvif_generic import service_urls, stream_uri
from qa_tool.drivers.camera.soap import (
    CameraUnreachable,
    OnvifFault,
    SoapClient,
    credentials_rejected,
)

# Тесты окна держат превью выключенным: иначе оно ходило бы к виртуальным камерам и
# добавляло запросы, которые тесты проверок считают
ENABLED = True
PERIOD_S = 3.0  # между кадрами: превью для глаза, не видео
RETRY_S = 15.0  # после сбоя (камера не ответила, поток не открылся)
# Одна выборка в секунду, без ожидания «успокоения» картинки: превью не замер
TIMINGS = Timings(keyframe_wait_s=12.0, clip_s=1.0, settle_frames=2, settle_max_s=0.0)
SAMPLE_BYTES = 8 << 20


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def preview_uri(soap: SoapClient, camera: Camera) -> str:
    """Адрес RTSP последнего медиапрофиля камеры; пусто — камера адреса не сообщила.

    Raises:
        CameraUnreachable, OnvifFault: службы или профили не прочитаны.
    """
    services = service_urls(soap, camera)
    media2 = "media2" in services
    if not media2 and "media" not in services:
        raise OnvifFault("камера не объявляет службу медиа (Media или Media2)")
    url, prefix = (services["media2"], "tr2") if media2 else (services["media"], "trt")
    answer = soap.call(url, f"<{prefix}:GetProfiles/>")
    tokens = [e.get("token", "") for e in answer.iter() if _local(e.tag) == "Profiles"]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise OnvifFault("GetProfiles не вернул ни одного медиапрофиля")
    return stream_uri(soap, url, media2, tokens[-1])


def _key(camera: Camera, password: str) -> tuple:
    """Камера и данные входа: смена пароля в таблице снимает запрет после отказа."""
    digest = hashlib.sha256(password.encode()).hexdigest()
    return camera.address, camera.port, camera.rtsp_port, camera.username, digest


class PreviewWorker(QObject):
    """Фоновая нить превью одной камеры за раз; сигналы приходят в нить окна.

    `watch()` выбирает камеру, `set_paused()` ставит и снимает паузу, `stop()` — при закрытии.
    """

    frame = Signal(int, bytes)  # место, JPEG
    status = Signal(int, str)  # место, почему кадра нет

    def __init__(self, place: Place) -> None:
        super().__init__()
        self.place = place
        self._camera: Camera | None = None
        self._paused = True
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._uris: dict[tuple, str] = {}
        self._refused: dict[tuple, str] = {}
        self._thread = threading.Thread(target=self._loop, name="qa-preview", daemon=True)
        self._thread.start()

    def watch(self, camera: Camera | None) -> None:
        """Показывать эту камеру; None — никакую."""
        with self._lock:
            self._camera = camera
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        """Пауза: идёт прогон или экран не виден."""
        with self._lock:
            self._paused = paused
        self._wake.set()

    def stop(self) -> None:
        """Остановить нить; снимок, который уже идёт, доснимется и не будет показан."""
        self._stopped.set()
        self._wake.set()

    def _target(self) -> Camera | None:
        with self._lock:
            return None if self._paused or not ENABLED else self._camera

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _loop(self) -> None:
        while not self._stopped.is_set():
            camera = self._target()
            if camera is None:
                self._sleep(3600)
                continue
            delay = self._shoot(camera)
            if self._target() == camera:
                self._sleep(delay)

    def _shoot(self, camera: Camera) -> float:
        """Один кадр камеры; вернуть паузу до следующего."""
        try:
            password = get_password(camera)
        except ConfigError:
            self.status.emit(camera.slot, "нет пароля камеры — впишите его на экране «Камеры»")
            return RETRY_S
        key = _key(camera, password)
        if key in self._refused:
            self.status.emit(camera.slot, self._refused[key])
            return 3600
        ctx = CheckContext(camera=camera, password=password, place=self.place)
        try:
            uri = self._uris.get(key) or preview_uri(SoapClient(camera.username, password), camera)
            if not uri.startswith("rtsp://"):
                self.status.emit(camera.slot, "камера не сообщила адрес видео RTSP")
                return RETRY_S
            self._uris[key] = uri
            source = RtspFrameSource(ctx, uri, TIMINGS, SAMPLE_BYTES, IMAGE_PREFIX)
            capture = source.capture()
        except OnvifFault as e:
            if credentials_rejected(e):
                return self._refuse(camera, key, "камера не приняла логин или пароль (ONVIF)")
            self.status.emit(camera.slot, f"ONVIF: {e}")
            return RETRY_S
        except StreamRefused as e:
            return self._refuse(camera, key, f"камера не приняла логин или пароль: {e}")
        except CameraUnreachable as e:
            self.status.emit(camera.slot, f"камера не отвечает: {e}")
            return RETRY_S
        # поток, декодер, ffmpeg: у каждого свои ошибки, а итог один — подождать и повторить
        except Exception as e:
            self.status.emit(camera.slot, f"кадр не получен: {e}")
            return RETRY_S
        if self._target() == camera:
            self.frame.emit(camera.slot, capture.preview_jpeg)
        return PERIOD_S

    def _refuse(self, camera: Camera, key: tuple, reason: str) -> float:
        self._refused[key] = (
            f"{reason}. Превью остановлено, чтобы не тратить попытки входа: проверьте пароль "
            "на экране «Камеры»"
        )
        self.status.emit(camera.slot, self._refused[key])
        return 3600
