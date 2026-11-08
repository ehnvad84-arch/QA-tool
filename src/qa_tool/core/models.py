"""Модели стенда, сценария и порогов. Поля — feature-spec.md skeleton, «Данные»."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

MAX_SLOTS = 8
RUVER = "ruver"


class ConfigError(ValueError):
    """Ошибка в файлах настроек; текст показывается инженеру как есть."""


class Place(StrEnum):
    """Место стенда: от него зависит, какие проверки доступны."""

    COLOR_BOX = "color_box"
    DARK_ROOM = "dark_room"

    @property
    def title(self) -> str:
        """Название места для экрана и протокола."""
        return {"color_box": "цветовой бокс", "dark_room": "тёмная комната"}[self.value]


@dataclass
class Camera:
    """Камера на месте стенда. Пароля здесь нет: он в системном хранилище паролей."""

    slot: int
    address: str
    username: str
    port: int = 80
    rtsp_port: int = 554
    vendor: str = "generic"
    name: str = ""
    # Адрес камеры в её собственной сети, если стенд ходит к ней через туннель или NAT: камера
    # называет им свои службы. Только псевдоним для разбора ответов, соединений на него нет.
    lan_address: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.slot <= MAX_SLOTS:
            raise ConfigError(f"Место {self.slot}: допустимы места 1–{MAX_SLOTS}")
        if not self.address:
            raise ConfigError(f"Место {self.slot}: не указан адрес камеры")
        if set(self.lan_address) & set("/@ :\\"):
            raise ConfigError(f"Место {self.slot}: адрес за туннелем — только IPv4 или имя хоста")


@dataclass
class Stand:
    """Стенд: место, сетевая карта к камерам, папка прогонов и до 8 камер на местах."""

    place: Place
    runs_dir: Path
    cameras: list[Camera] = field(default_factory=list)
    interface: str = ""  # IP сетевой карты, смотрящей в сеть камер; пусто — по умолчанию

    def __post_init__(self) -> None:
        cameras, self.cameras = self.cameras, []
        for camera in cameras:
            self.add_camera(camera)

    def free_slots(self) -> list[int]:
        """Свободные места по возрастанию."""
        taken = {c.slot for c in self.cameras}
        return [s for s in range(1, MAX_SLOTS + 1) if s not in taken]

    def remove_camera(self, slot: int) -> None:
        """Снять камеру с места; пустое место — без ошибки."""
        self.cameras = [c for c in self.cameras if c.slot != slot]

    def camera_at(self, slot: int) -> Camera | None:
        """Камера на месте или None."""
        return next((c for c in self.cameras if c.slot == slot), None)

    def add_camera(self, camera: Camera) -> None:
        """Поставить камеру на её место.

        Raises:
            ConfigError: все 8 мест заняты или место камеры уже занято.
        """
        if not self.free_slots():
            raise ConfigError(f"Свободных мест нет: на стенде уже {MAX_SLOTS} камер")
        if camera.slot not in self.free_slots():
            raise ConfigError(f"Место {camera.slot} уже занято")
        self.cameras.append(camera)
        self.cameras.sort(key=lambda c: c.slot)


@dataclass
class Scenario:
    """Что проверяем. `checks=None` — все проверки, доступные камере."""

    checks: list[str] | None = None
    allow_writes: bool = False
    allow_destructive: bool = False
    stress_minutes: int = 60


@dataclass(frozen=True)
class Threshold:
    """Порог «прошла / не прошла»: значение, единица и источник (печатается в протоколе)."""

    value: float
    unit: str
    source: str
