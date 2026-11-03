"""Реестр проверок и их результаты.

Проверка — функция `(ctx: CheckContext) -> CheckResult`, зарегистрированная декоратором
`@REGISTRY.check(...)`. Правила never.md: «прошла» без доказательства — это «ошибка стенда»;
разрушающие и изменяющие проверки идут только по галочкам сценария.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

from qa_tool.core.models import Camera, Place, Scenario, Threshold
from qa_tool.core.taboo import TabooViolation

UNREACHABLE = "unreachable"
"""Ключ в CheckResult.data: камера не ответила по сети (не ошибка проверки, а отсутствие связи)."""


class Status(StrEnum):
    PASSED = "прошла"
    FAILED = "не прошла"
    SKIPPED = "пропущена"
    STAND_ERROR = "ошибка стенда"


class Phase(IntEnum):
    """Порядок фаз прогона (AD-2)."""

    NETWORK = 1
    SYNC = 2
    SEQUENTIAL = 3
    STRESS = 4

    @property
    def title(self) -> str:
        return {1: "сетевая", 2: "синхронная стенда", 3: "поочерёдная", 4: "стресс"}[self.value]


class Severity(StrEnum):
    BLOCKER = "блокер"
    MAJOR = "серьёзная"
    MINOR = "мелкая"


@dataclass
class Evidence:
    """Доказательство: обмен с камерой, кадр, видео. Секреты вырезаются при сохранении."""

    name: str
    content: str | bytes
    kind: str = "text"


@dataclass
class CheckResult:
    status: Status
    reason: str = ""
    expected: str = ""
    actual: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    threshold: Threshold | None = None
    data: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    @classmethod
    def skipped(cls, reason: str) -> "CheckResult":
        return cls(Status.SKIPPED, reason=reason)

    @classmethod
    def stand_error(cls, reason: str) -> "CheckResult":
        return cls(Status.STAND_ERROR, reason=reason)


@dataclass
class CheckContext:
    camera: Camera
    password: str
    place: Place
    thresholds: dict[str, Threshold] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


CheckFunc = Callable[[CheckContext], CheckResult]


@dataclass(frozen=True)
class CheckSpec:
    id: str
    module: str
    title: str
    description: str
    func: CheckFunc
    phase: Phase = Phase.NETWORK
    resources: frozenset[str] = frozenset()
    vendors: frozenset[str] | None = None  # None — любой производитель
    places: frozenset[Place] | None = None  # None — любое место стенда
    writes: bool = False
    destructive: bool = False
    severity: Severity = Severity.MAJOR
    order: int = 100  # порядок внутри фазы; CORE-CONNECT-1 — первым
    timeout_s: float = 120.0  # дольше — проверка считается зависшей

    def applies_to(self, camera: Camera) -> bool:
        return self.vendors is None or camera.vendor.lower() in self.vendors


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, CheckSpec] = {}

    def check(self, id: str, module: str, title: str, description: str, **options: Any):
        def register(func: CheckFunc) -> CheckFunc:
            if id in self._specs:
                raise ValueError(f"Проверка {id} уже зарегистрирована")
            options_ = dict(options)
            for key in ("resources", "vendors", "places"):
                if options_.get(key) is not None:
                    options_[key] = frozenset(options_[key])
            self._specs[id] = CheckSpec(id, module, title, description, func, **options_)
            return func

        return register

    def all(self) -> list[CheckSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.phase, s.order, s.module, s.id))

    def get(self, id: str) -> CheckSpec:
        return self._specs[id]

    def plan_for(
        self, camera: Camera, scenario: Scenario, place: Place
    ) -> list[tuple[CheckSpec, str | None]]:
        """Проверки камеры по сценарию: (проверка, причина пропуска или None).

        Проверки чужого производителя в план не попадают вовсе (AD-4).
        """
        planned = []
        for spec in self.all():
            if not spec.applies_to(camera):
                continue
            if scenario.checks is not None and spec.id not in scenario.checks:
                continue
            planned.append((spec, skip_reason(spec, scenario, place)))
        return planned


def skip_reason(spec: CheckSpec, scenario: Scenario, place: Place) -> str | None:
    if spec.destructive and not scenario.allow_destructive:
        return "не разрешено сценарием: разрушающая проверка"
    if spec.writes and not scenario.allow_writes:
        return "не разрешено сценарием: изменяющая проверка"
    if spec.places is not None and place not in spec.places:
        needed = ", ".join(sorted(p.title for p in spec.places))
        return f"нужно другое место стенда: {needed}"
    return None


def run_check(spec: CheckSpec, ctx: CheckContext, skip: str | None = None) -> CheckResult:
    if skip is not None:
        return CheckResult.skipped(skip)
    start = time.monotonic()
    try:
        result = spec.func(ctx)
    except TabooViolation as e:  # проверка попыталась нарушить табу — ошибка программы
        result = CheckResult.stand_error(f"запрещённая операция не отправлена: {e}")
    except Exception as e:  # сбой самой проверки — не вина камеры
        result = CheckResult.stand_error(f"сбой проверки: {type(e).__name__}: {e}")
    if result.status is Status.PASSED and not result.evidence:
        result = CheckResult.stand_error("проверка не сохранила доказательство")
    result.duration_s = time.monotonic() - start
    return result


REGISTRY = Registry()
"""Общий реестр: модули из qa_tool.checks регистрируют в него свои проверки."""
