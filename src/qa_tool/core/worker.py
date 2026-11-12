"""Рабочий процесс одной камеры. Запускается планировщиком через multiprocessing (spawn).

Сообщения в координатор (outbox): кортежи (вид, место, ...):
  started(check_id, время) · result(check_id, CheckResult, время начала) ·
  lock(check_id, ресурсы) · release(check_id) · barrier(check_id) · phase_done(фаза) · lost(причина)
Ответы координатора (inbox): ("phase", фаза) · ("grant", check_id) · ("go", check_id) ·
  ("abort", check_id) · ("quit",)
"""

import datetime
import importlib
from dataclasses import dataclass, field
from typing import Any

from qa_tool.core.checks import (
    CREDENTIALS_REJECTED,
    FIRST_CHECK,
    PREVIOUS_RESULTS,
    UNREACHABLE,
    CheckContext,
    CheckResult,
    Phase,
    Registry,
    Status,
    run_check,
)
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.probe import tcp_open

STOPPED = "прогон остановлен"
LOST_DURING_CHECK = "камера перестала отвечать во время проверки (сбой камеры или прошивки)"


def resolve_registry(ref: str) -> Registry:
    """Реестр по ссылке «модуль:имя»; вызываемая ссылка вызывается (так подгружаются модули)."""
    module_name, _, attr = ref.partition(":")
    target = getattr(importlib.import_module(module_name), attr)
    return target() if callable(target) and not isinstance(target, Registry) else target


@dataclass
class WorkerArgs:
    """Всё, что рабочий процесс камеры получает при запуске (передаётся через spawn)."""

    camera: Camera
    password: str
    place: Place
    plan: list[tuple[str, str | None]]  # (check_id, причина пропуска)
    registry_ref: str
    sync_checks: set[str] = field(default_factory=set)
    thresholds: dict[str, Threshold] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    allow_writes: bool = False
    allow_destructive: bool = False
    stress_minutes: int = 60


def camera_alive(camera: Camera, timeout: float = 1.0, attempts: int = 2) -> bool:
    """Отвечает ли камера на TCP-порт HTTP; две попытки, чтобы не путать сбой с пропажей."""
    return any(tcp_open(camera.address, camera.port, timeout) for _ in range(attempts))


def wait_reply(inbox, expected: str) -> str:
    """Ответ координатора на просьбу: expected, «abort» или «quit». Чужие сообщения не считаются
    разрешением — проверка не запустится без блокировки ресурса."""
    while True:
        kind = inbox.get()[0]
        if kind in (expected, "abort", "quit"):
            return kind


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def worker_main(args: WorkerArgs, inbox, outbox, stop_event) -> None:
    """Рабочий процесс одной камеры: выполняет фазы по команде координатора.

    Связь с координатором — две очереди; формат сообщений описан в начале модуля.
    Камера, пропавшая из сети, сообщает «lost» и ждёт снятия, а не выходит сама.
    """
    registry = resolve_registry(args.registry_ref)
    slot = args.camera.slot
    ctx = CheckContext(
        args.camera,
        args.password,
        args.place,
        args.thresholds,
        dict(args.extra),
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        stress_minutes=args.stress_minutes,
        stop_requested=stop_event.is_set,
    )
    by_phase: dict[Phase, list[tuple[str, str | None]]] = {}
    for check_id, skip in args.plan:
        by_phase.setdefault(registry.get(check_id).phase, []).append((check_id, skip))
    alive_checked = False  # камера отвечала хотя бы раз — дальше пропажа = «нет связи»
    unreachable_from_start = False
    # Решение директора 2026-11-03: неверный логин или пароль — ошибка стенда. Остальные
    # проверки с ним всё равно получили бы отказ и стали бы ложными «не прошла» с задачами.
    rejected_from_start = False

    previous = ctx.extra.setdefault(PREVIOUS_RESULTS, {})

    def send_result(check_id: str, result: CheckResult, started: str) -> None:
        outbox.put(("result", slot, check_id, result, started))
        rows = [(c.check_id, c.result) for c in result.children] or [(check_id, result)]
        for row_id, row in rows:
            previous[row_id] = (row.status.value, row.reason)

    def lost() -> None:
        outbox.put(("lost", slot, "нет связи"))
        # не выходим сами: иначе координатор может увидеть «процесс умер» раньше,
        # чем прочтёт «нет связи», и запишет не ту причину. Ждём, пока он нас снимет.
        while inbox.get()[0] != "quit":
            pass

    while True:
        message = inbox.get()
        if message[0] == "quit":
            return
        phase = Phase(message[1])
        for check_id, skip in by_phase.get(phase, []):
            spec = registry.get(check_id)
            started = _now()
            if stop_event.is_set():
                send_result(check_id, CheckResult.skipped(STOPPED), started)
                continue
            if skip is not None:
                send_result(check_id, run_check(spec, ctx, skip), started)
                continue
            if unreachable_from_start:
                reason = f"камера не на связи с начала прогона (см. {FIRST_CHECK})"
                send_result(check_id, CheckResult.skipped(reason), started)
                continue
            if rejected_from_start:
                reason = f"камера не приняла логин или пароль (см. {FIRST_CHECK})"
                send_result(check_id, CheckResult.skipped(reason), started)
                continue
            liveness = args.extra.get("liveness", True)
            if alive_checked and liveness and not camera_alive(args.camera):
                lost()
                return
            if check_id in args.sync_checks or spec.resources:
                if check_id in args.sync_checks:
                    outbox.put(("barrier", slot, check_id))
                    reply = wait_reply(inbox, "go")
                else:
                    outbox.put(("lock", slot, check_id, sorted(spec.resources)))
                    reply = wait_reply(inbox, "grant")
                if reply == "quit":
                    return
                if reply == "abort":
                    send_result(check_id, CheckResult.skipped(STOPPED), started)
                    continue
            started = _now()
            outbox.put(("started", slot, check_id, started))
            result = run_check(spec, ctx)
            if spec.resources and check_id not in args.sync_checks:
                outbox.put(("release", slot, check_id))
            # камера отвечала до этой проверки или в её начале (порт HTTP был открыт)
            was_reachable = alive_checked or not result.data.get(UNREACHABLE)
            if not alive_checked:
                unreachable_from_start = check_id == FIRST_CHECK and bool(
                    result.data.get(UNREACHABLE)
                )
                alive_checked = not unreachable_from_start
                rejected_from_start = check_id == FIRST_CHECK and bool(
                    result.data.get(CREDENTIALS_REJECTED)
                )
            if (
                was_reachable
                and result.status in (Status.FAILED, Status.STAND_ERROR)
                and liveness
                and not camera_alive(args.camera)
            ):
                # Решение директора 2026-11-03: камера, переставшая отвечать во время проверки, —
                # сбой камеры или прошивки, задача программистам. Остальные её проверки после
                # этого провести нельзя — они получают «ошибка стенда: нет связи».
                failed = CheckResult(
                    Status.FAILED,
                    reason=LOST_DURING_CHECK,
                    expected=result.expected,
                    actual=f"{result.actual or result.reason}; камера перестала отвечать",
                    evidence=result.evidence,
                )
                send_result(check_id, failed, started)
                lost()
                return
            send_result(check_id, result, started)
        outbox.put(("phase_done", slot, int(phase)))
