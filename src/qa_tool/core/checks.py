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
from qa_tool.core.secrets import redact
from qa_tool.core.taboo import TabooViolation

PREVIOUS_RESULTS = "previous_results"
"""Ключ ctx.extra: итоги уже выполненных строк этой камеры {ID: (статус, причина)} —
для проверок, которые сводят результаты других (матрица профилей ONVIF)."""

UNREACHABLE = "unreachable"
"""Ключ в CheckResult.data: камера не ответила по сети (не ошибка проверки, а отсутствие связи)."""

FIRST_CHECK = "CORE-CONNECT-1"
"""Первая проверка каждой камеры: связь и паспорт."""

CREDENTIALS_REJECTED = "credentials_rejected"
"""Ключ в CheckResult.data: камера не приняла логин или пароль места — ошибка стенда."""


class Status(StrEnum):
    """Итог проверки. «Ошибка стенда» и «пропущена» в задачи программистам не попадают."""

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
        """Название фазы для экрана и протокола."""
        return {1: "сетевая", 2: "синхронная стенда", 3: "поочерёдная", 4: "стресс"}[self.value]


class Severity(StrEnum):
    """Важность задачи программистам по проверке, которая не прошла."""

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
    """Результат проверки одной камеры: статус, причина, ожидание, факт, порог, доказательства."""

    status: Status
    reason: str = ""
    expected: str = ""
    actual: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    threshold: Threshold | None = None
    data: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    children: list["ChildResult"] = field(default_factory=list)

    @classmethod
    def skipped(cls, reason: str) -> "CheckResult":
        """Проверка не запускалась; причина печатается в протоколе."""
        return cls(Status.SKIPPED, reason=reason)

    @classmethod
    def stand_error(cls, reason: str) -> "CheckResult":
        """Проверку не удалось довести до конца по вине стенда, а не камеры."""
        return cls(Status.STAND_ERROR, reason=reason)


@dataclass
class ChildResult:
    """Один результат составной проверки — отдельная строка протокола со своим ID.

    Составная проверка (например, прогон onvif-tt) выдаёт заранее неизвестный набор тестов:
    регистрировать каждый в реестре нельзя, состав зависит от камеры.
    """

    check_id: str
    title: str
    result: CheckResult
    description: str = ""
    severity: "Severity | None" = None


@dataclass
class CheckContext:
    """Всё, что проверка получает о камере и стенде. Пароль — только в памяти рабочего процесса."""

    camera: Camera
    password: str
    place: Place
    thresholds: dict[str, Threshold] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # галочки сценария: составным проверкам (onvif-tt) решать, что из своего запускать
    allow_writes: bool = False
    allow_destructive: bool = False
    stress_minutes: int = 60
    # долгая составная проверка спрашивает здесь: остановка не снимает её посреди записи
    stop_requested: Callable[[], bool] = lambda: False


CheckFunc = Callable[[CheckContext], CheckResult]


@dataclass(frozen=True)
class CheckSpec:
    """Описание зарегистрированной проверки: где, когда и с какими ресурсами она идёт."""

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
    stress: bool = False  # идёт только с галочкой стресс-теста, длится сколько задано в сценарии
    severity: Severity = Severity.MAJOR
    order: int = 100  # порядок внутри фазы; CORE-CONNECT-1 — первым
    timeout_s: float = 120.0  # дольше — проверка считается зависшей
    group: str = ""  # подгруппа внутри модуля на экране «Сценарий»; пусто — прямо в модуле
    help_id: str = ""  # раздел справки F1 для строки проверки; пусто — общий раздел «Проверки»

    def time_limit_s(self, scenario: Scenario) -> float:
        """Срок, после которого проверка считается зависшей, с учётом длительности стресс-теста."""
        return self.timeout_s + (scenario.stress_minutes * 60 if self.stress else 0)

    def applies_to(self, camera: Camera) -> bool:
        """Подходит ли проверка производителю камеры (AD-4: адаптерные — только своим)."""
        return self.vendors is None or camera.vendor.lower() in self.vendors


class Registry:
    """Реестр проверок. Модули регистрируют в нём функции декоратором `check`."""

    def __init__(self) -> None:
        self._specs: dict[str, CheckSpec] = {}

    def check(self, id: str, module: str, title: str, description: str, **options: Any):
        """Декоратор регистрации проверки.

        Args:
            id: идентификатор `<МОДУЛЬ>-<ГРУППА>-<N>`, уникальный в реестре.
            module: модуль проверок (core, onvif, api …) — раздел протокола.
            title: название для экрана и протокола.
            description: «что делаем, что ожидаем» — уходит в протокол и шаги задачи.
            **options: поля CheckSpec (phase, resources, vendors, places, writes,
                destructive, severity, order, timeout_s, group, help_id).

        Raises:
            ValueError: проверка с таким id уже зарегистрирована.
        """

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
        """Все проверки в порядке прогона: фаза, порядок, модуль, id."""
        return sorted(self._specs.values(), key=lambda s: (s.phase, s.order, s.module, s.id))

    def get(self, id: str) -> CheckSpec:
        """Проверка по id; KeyError, если такой нет."""
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
            # CORE-CONNECT-1 — всегда: без неё нет паспорта в шапке протокола, а неверный пароль
            # стал бы ложными «не прошла» у остальных проверок (worker.py).
            if (
                scenario.checks is not None
                and spec.id not in scenario.checks
                and spec.id != FIRST_CHECK
            ):
                continue
            planned.append((spec, skip_reason(spec, scenario, place)))
        return planned


def skip_reason(spec: CheckSpec, scenario: Scenario, place: Place) -> str | None:
    """Почему проверку нельзя запускать по сценарию и месту стенда; None — можно."""
    if spec.destructive and not scenario.allow_destructive:
        return "не разрешено сценарием: разрушающая проверка"
    if spec.writes and not scenario.allow_writes:
        return "не разрешено сценарием: изменяющая проверка"
    if spec.stress and not scenario.allow_stress:
        return "не разрешено сценарием: стресс-тест выключен"
    if spec.places is not None and place not in spec.places:
        needed = ", ".join(sorted(p.title for p in spec.places))
        return f"нужно другое место стенда: {needed}"
    return None


def run_check(spec: CheckSpec, ctx: CheckContext, skip: str | None = None) -> CheckResult:
    """Выполнить проверку и привести результат к правилам never.md.

    «Прошла» без доказательства становится «ошибкой стенда»; исключение проверки —
    тоже «ошибка стенда» (пароль из текста вырезан); попытка нарушить табу —
    «ошибка стенда», запрос при этом в сеть не ушёл.

    Args:
        spec: проверка из реестра.
        ctx: камера, пароль, место стенда, пороги.
        skip: причина пропуска; если задана, проверка не вызывается.

    Returns:
        Результат с заполненной длительностью.
    """
    if skip is not None:
        return CheckResult.skipped(skip)
    start = time.monotonic()
    try:
        result = spec.func(ctx)
    except TabooViolation as e:  # проверка попыталась нарушить табу — ошибка программы
        result = CheckResult.stand_error(f"запрещённая операция не отправлена: {e}")
    except Exception as e:  # сбой самой проверки — не вина камеры
        result = CheckResult.stand_error(
            redact(f"сбой проверки: {type(e).__name__}: {e}", [ctx.password])
        )
    if result.children:
        for child in result.children:  # правило never.md — для каждой строки протокола
            if child.result.status is Status.PASSED and not child.result.evidence:
                child.result = CheckResult.stand_error("проверка не сохранила доказательство")
    elif result.status is Status.PASSED and not result.evidence:
        result = CheckResult.stand_error("проверка не сохранила доказательство")
    scrub(result, [ctx.password])
    result.duration_s = time.monotonic() - start
    return result


def scrub(result: CheckResult, known_secrets: list[str]) -> None:
    """Вырезать известные секреты из текстов результата, строк и текстовых доказательств.

    `RunStore` знает только общие признаки секретов, а камера может вывести пароль как есть:
    повторить в ответе или вывести в консоль браузера. Известный пароль есть только здесь.

    Args:
        result: результат проверки; меняется на месте.
        known_secrets: пароль камеры, куки сессии.
    """
    known = [secret for secret in known_secrets if secret]
    for item in (result, *(child.result for child in result.children)):
        item.reason = redact(item.reason, known)
        item.expected = redact(item.expected, known)
        item.actual = redact(item.actual, known)
        for evidence in item.evidence:
            if isinstance(evidence.content, str):
                evidence.content = redact(evidence.content, known)
    for child in result.children:
        child.title = redact(child.title, known)


REGISTRY = Registry()
"""Общий реестр: модули из qa_tool.checks регистрируют в него свои проверки."""
