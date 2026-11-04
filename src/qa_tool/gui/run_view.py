"""Состояние экрана «Прогон» по событиям планировщика, без Qt (вариант D, docs/work/gui-layout).

Окно только рисует то, что собрано здесь: процент и итог места, цвет точки, ход проверки,
задачи программистам. Поэтому сценарии S1–S6 спеки проверяются быстрыми тестами без окна.
События приходят в поток окна по одному и в порядке отправки (сигнал Qt), блокировок не нужно.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from qa_tool.core.checks import Status
from qa_tool.core.models import Camera
from qa_tool.core.scheduler import Event
from qa_tool.core.worker import LOST_DURING_CHECK, STOPPED
from qa_tool.report.model import SEVERITY_ORDER

RUNNING = "идёт"

# Итог составной проверки — худший из её тестов. «Пропущена» хуже «прошла» не считается:
# у onvif-tt часть тестов всегда пропущена (функции нет у камеры), и итог «пропущена» у набора,
# где 150 тестов прошли, вводил бы в заблуждение. Набор целиком пропущен — значит, пропущен.
_WORST = (Status.FAILED.value, Status.STAND_ERROR.value, Status.PASSED.value, Status.SKIPPED.value)


class Dot(StrEnum):
    """Цвет точки места в списке камер; рядом всегда есть итог словами."""

    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"
    OK = "ok"
    STAND = "stand"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Число со словом в нужной форме: 1 ошибка, 2 ошибки, 5 ошибок."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


@dataclass
class Step:
    """Строка блока «Ход проверки»: одна проверка сценария (составная — одной строкой)."""

    time: str
    check_id: str
    title: str
    state: str = RUNNING
    reason: str = ""
    parts: list[str] = field(default_factory=list)  # итоги тестов составной проверки

    @property
    def label(self) -> str:
        """Название для экрана; у составной — с числом тестов («onvif-tt: 161 тест»)."""
        if not self.parts:
            return self.title
        return f"{self.title}: {plural(len(self.parts), 'тест', 'теста', 'тестов')}"


@dataclass
class Task:
    """Карточка «Задачи программистам»: проверка «не прошла» с важностью и причиной."""

    check_id: str
    title: str
    severity: str
    reason: str
    phase: int = 0
    module: str = ""


@dataclass
class SlotView:
    """Всё, что окно показывает о месте стенда: строка списка и карточка камеры."""

    slot: int
    camera: Camera | None = None
    total: int = 0
    done: int = 0
    passport: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: {s.value: 0 for s in Status})
    steps: list[Step] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    started: bool = False
    finished: bool = False
    stopped_responding: bool = False
    lost_reason: str = ""
    first_stand_error: str = ""
    stopped: int = 0  # проверки, пропущенные из-за остановки прогона

    @property
    def complete(self) -> bool:
        """Весь план выполнен; камера может закончить раньше остальных."""
        return self.total > 0 and self.done >= self.total and not self.stopped

    @property
    def maker(self) -> str:
        """Производитель из паспорта; до паспорта — адрес камеры из стенда."""
        if self.passport.get("manufacturer"):
            return self.passport["manufacturer"]
        return self.camera.address if self.camera else "свободно"

    @property
    def model(self) -> str:
        """Производитель и модель для шапки карточки."""
        parts = (self.passport.get("manufacturer", ""), self.passport.get("model", ""))
        return " ".join(p for p in parts if p) or self.maker

    @property
    def percent(self) -> int:
        """Сколько проверок сценария выполнено, в процентах (составная — одна проверка).

        Пропущенные из-за остановки не считаются: иначе остановленный прогон показывал бы 100 %.
        """
        return min(100, round(100 * (self.done - self.stopped) / self.total)) if self.total else 0

    @property
    def dot(self) -> Dot:
        """Цвет точки: сбой камеры и «не прошла» важнее ошибки стенда, она — важнее хода."""
        if self.camera is None:
            return Dot.IDLE
        if self.stopped_responding or self.counts[Status.FAILED.value]:
            return Dot.FAILED
        if self.lost_reason or self.counts[Status.STAND_ERROR.value]:
            return Dot.STAND
        if self.complete:
            return Dot.OK
        return Dot.RUNNING if self.started and not self.finished else Dot.IDLE

    @property
    def summary(self) -> str:
        """Итог места одной строкой для списка камер."""
        if self.camera is None:
            return "свободно"
        if self.stopped_responding:
            return "не отвечает"
        failed = self.counts[Status.FAILED.value]
        stand = self.counts[Status.STAND_ERROR.value]
        if failed:
            text = plural(failed, "ошибка", "ошибки", "ошибок")
            return f"{text}, ошибок стенда {stand}" if stand else text
        if self.lost_reason:
            return self.lost_reason
        if stand:
            return f"ошибка стенда: {self.first_stand_error}"
        if self.finished and not self.complete:
            return "остановлен, ошибок нет"
        if not self.started:
            return "ожидание"
        if not self.done and not any(self.counts.values()):
            return "подключение"
        return "ошибок нет"

    def sorted_tasks(self) -> list[Task]:
        """Задачи как в таблице протокола: по важности, затем по фазе, модулю и ID проверки."""
        return sorted(
            self.tasks,
            key=lambda t: (SEVERITY_ORDER.get(t.severity, 9), t.phase, t.module, t.check_id),
        )


class RunView:
    """Состояние всех мест стенда за прогон; `apply` возвращает место, которое изменилось."""

    def __init__(self, slots: int):
        self.slots = {n: SlotView(n) for n in range(1, slots + 1)}
        self.phase = ""
        self.finished = False

    def reset(self, slot: int, camera: Camera | None, total: int) -> None:
        """Начать место заново перед прогоном: камера стенда и число проверок её плана."""
        self.slots[slot] = SlotView(slot, camera=camera, total=total)
        self.phase, self.finished = "", False

    def abort(self, reason: str) -> None:
        """Прогон оборвался посреди проверок: незавершённые места не должны выглядеть идущими."""
        self.finished = True
        for view in self.slots.values():
            if view.camera is None:
                continue
            view.finished = True
            if view.started and not view.complete and not view.lost_reason:
                view.lost_reason = f"ошибка стенда: {reason}"
            for step in view.steps:
                if step.state == RUNNING:
                    step.state, step.reason = Status.STAND_ERROR.value, reason

    def apply(self, event: Event, at: datetime) -> int | None:
        """Учесть событие планировщика; `at` — когда оно пришло в окно (время строки хода)."""
        if event.kind == "phase":
            self.phase = event.title
            return None
        if event.kind == "finished":
            self.finished = True
            for view in self.slots.values():
                if view.camera is not None:
                    view.finished = True
            return None
        view = self.slots.get(event.slot)
        if view is None:
            return None
        if event.kind == "passport":
            view.passport = dict(event.passport)
        elif event.kind == "started":
            view.started = True
            view.steps.append(Step(at.strftime("%H:%M"), event.check_id, event.title))
        elif event.kind == "result":
            self._result(view, event)
        elif event.kind == "done":
            step = self._running(view)
            if step is not None:
                step.state = next((s for s in _WORST if s in step.parts), Status.SKIPPED.value)
            view.done += 1
        elif event.kind == "lost":
            view.lost_reason = event.detail
            for step in view.steps:
                if step.state == RUNNING:
                    step.state, step.reason = Status.STAND_ERROR.value, event.detail
        return event.slot

    def _result(self, view: SlotView, event: Event) -> None:
        view.started = True
        view.counts[event.status] = view.counts.get(event.status, 0) + 1
        if event.status == Status.SKIPPED.value and event.detail == STOPPED:
            view.stopped += 1
        if event.status == Status.STAND_ERROR.value and not view.first_stand_error:
            view.first_stand_error = event.detail
        if event.status == Status.FAILED.value:
            if event.detail == LOST_DURING_CHECK:
                view.stopped_responding = True
            view.tasks.append(
                Task(
                    event.check_id,
                    event.title,
                    event.severity,
                    event.detail,
                    event.phase,
                    event.module,
                )
            )
        step = self._running(view)
        if event.part:
            if step is not None:
                step.parts.append(event.status)
            return
        view.done += 1
        if step is not None and step.check_id == event.check_id:
            step.state, step.reason = event.status, event.detail
        else:
            # итог без «started»: проверка пропущена до запуска (остановка, нет связи с начала)
            view.steps.append(Step("", event.check_id, event.title, event.status, event.detail))

    @staticmethod
    def _running(view: SlotView) -> Step | None:
        return next((s for s in reversed(view.steps) if s.state == RUNNING), None)
