"""Модели стенда, сценария и порогов. Поля — feature-spec.md skeleton, «Данные»."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

MAX_SLOTS = 8
RUVER = "ruver"


class ConfigError(ValueError):
    """Ошибка в файлах настроек; текст показывается инженеру как есть."""


class Place(StrEnum):
    COLOR_BOX = "color_box"
    DARK_ROOM = "dark_room"

    @property
    def title(self) -> str:
        return {"color_box": "цветовой бокс", "dark_room": "тёмная комната"}[self.value]


@dataclass
class Camera:
    slot: int
    address: str
    username: str
    port: int = 80
    rtsp_port: int = 554
    vendor: str = "generic"
    name: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.slot <= MAX_SLOTS:
            raise ConfigError(f"Место {self.slot}: допустимы места 1–{MAX_SLOTS}")
        if not self.address:
            raise ConfigError(f"Место {self.slot}: не указан адрес камеры")

    @property
    def is_ruver(self) -> bool:
        return self.vendor.lower() == RUVER


@dataclass
class Stand:
    place: Place
    runs_dir: Path
    cameras: list[Camera] = field(default_factory=list)
    interface: str = ""  # IP сетевой карты, смотрящей в сеть камер; пусто — по умолчанию

    def __post_init__(self) -> None:
        cameras, self.cameras = self.cameras, []
        for camera in cameras:
            self.add_camera(camera)

    def free_slots(self) -> list[int]:
        taken = {c.slot for c in self.cameras}
        return [s for s in range(1, MAX_SLOTS + 1) if s not in taken]

    def add_camera(self, camera: Camera) -> None:
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
    value: float
    unit: str
    source: str
