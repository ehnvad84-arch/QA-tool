"""Планировщик прогона (AD-2): процесс на камеру, фазы по порядку, барьер синхронной фазы,
блокировки общих ресурсов стенда. Результаты в базу пишет только он (координатор)."""

import multiprocessing as mp
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.checks import CheckResult, Phase, Status
from qa_tool.core.models import Scenario, Stand, Threshold
from qa_tool.core.secrets import get_password
from qa_tool.core.store import RunStore
from qa_tool.core.worker import STOPPED, WorkerArgs, resolve_registry, worker_main

DEFAULT_REGISTRY = "qa_tool.checks:registry"
FREEZE_GAP_S = 2.0  # цикл координатора крутится каждые 0,2 с; дольше 2 с — значит, он стоял


@dataclass
class Event:
    """Событие прогона для окна и командной строки: фаза, старт и итог проверки, потеря камеры."""

    kind: str  # вид события: phase, started, result, done, lost, finished
    slot: int = 0
    check_id: str = ""
    title: str = ""
    status: str = ""
    detail: str = ""
    phase: int = 0
    part: bool = False  # строка составной проверки: считается в итогах, но не в прогрессе


@dataclass
class _Camera:
    slot: int
    process: Any
    inbox: Any
    plan: list[tuple[str, str | None]]
    done: set[str] = field(default_factory=set)
    running: tuple[str, float] | None = None  # (check_id, крайний срок)
    alive: bool = True
    phase_done: bool = False


class Runner:
    """Прогон стенда (AD-2): процесс на камеру, фазы по порядку, барьер синхронной фазы,
    блокировки общих ресурсов стенда. Результаты в базу пишет только этот объект.

    Объект одноразовый: один `run()`. `stop()` можно вызывать из другого потока.
    """

    def __init__(
        self,
        stand: Stand,
        scenario: Scenario,
        thresholds: dict[str, Threshold] | None = None,
        registry_ref: str = DEFAULT_REGISTRY,
        on_event: Callable[[Event], None] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.stand, self.scenario = stand, scenario
        self.thresholds = thresholds or {}
        self.registry_ref = registry_ref
        self.registry = resolve_registry(registry_ref)
        self.on_event = on_event or (lambda _e: None)
        self.extra = extra or {}
        self._mp = mp.get_context("spawn")
        self._stop = self._mp.Event()
        self.store: RunStore | None = None

    def stop(self) -> None:
        """Остановить прогон: начатые проверки доработают, неначатые — «пропущена»."""
        self._stop.set()

    # --- запуск -------------------------------------------------------------------------------

    def run(self) -> RunStore:
        """Провести прогон до конца или до остановки.

        Пароли берутся до создания папки прогона, поэтому ошибка конфигурации не
        оставляет пустых прогонов.

        Returns:
            База прогона (открыта в вызывающем потоке).

        Raises:
            ConfigError: не задан пароль камеры.
            OSError, sqlite3.Error: не создать папку или базу прогона.
            keyring.errors.KeyringError: недоступно системное хранилище паролей.
        """
        passwords = {
            cam.slot: get_password(cam) for cam in self.stand.cameras
        }  # ConfigError — до создания папки прогона
        self.store = RunStore.create(self.stand, self.scenario)
        self.outbox = self._mp.Queue()
        plans = {
            cam.slot: [
                (s.id, skip)
                for s, skip in self.registry.plan_for(cam, self.scenario, self.stand.place)
            ]
            for cam in self.stand.cameras
        }
        self.sync_ids = {
            check_id
            for plan in plans.values()
            for check_id, skip in plan
            if skip is None and self.registry.get(check_id).phase is Phase.SYNC
        }
        self.cams: dict[int, _Camera] = {}
        for cam in self.stand.cameras:
            inbox = self._mp.Queue()
            args = WorkerArgs(
                camera=cam,
                password=passwords[cam.slot],
                place=self.stand.place,
                plan=plans[cam.slot],
                registry_ref=self.registry_ref,
                sync_checks=self.sync_ids,
                thresholds=self.thresholds,
                extra=self.extra,
            )
            process = self._mp.Process(
                target=worker_main, args=(args, inbox, self.outbox, self._stop), daemon=True
            )
            process.start()
            self.cams[cam.slot] = _Camera(cam.slot, process, inbox, plans[cam.slot])
        try:
            for phase in Phase:
                if self._stop.is_set():
                    break
                self._run_phase(phase)
        finally:
            self._shutdown()
        for c in self.cams.values():
            self._fill_missing(c, CheckResult.skipped(STOPPED))
        self.store.finish("остановлен" if self._stop.is_set() else "завершён")
        self.on_event(Event("finished", detail=self.store.run_id))
        return self.store

    def _run_phase(self, phase: Phase) -> None:
        self.on_event(Event("phase", phase=int(phase), title=phase.title))
        self.locks: dict[str, int] = {}  # ресурс → место
        self.lock_queue: list[tuple[int, str, list[str]]] = []
        self.barriers: dict[str, set[int]] = {}
        self.sync_holders: dict[str, set[int]] = {}  # синхронная проверка → кто ещё не закончил
        for c in self._alive():
            c.phase_done = False
            c.inbox.put(("phase", int(phase)))
        stop_sent = False
        last_tick = time.monotonic()
        while any(not c.phase_done for c in self._alive()):
            if self._stop.is_set() and not stop_sent:
                self._abort_waiting()
                stop_sent = True
            try:
                message = self.outbox.get(timeout=0.2)
            except queue.Empty:
                message = None
            last_tick = self._compensate_freeze(last_tick)
            if message is not None:
                self._handle(message)
            self._check_health()

    # --- сообщения рабочих --------------------------------------------------------------------

    def _handle(self, message: tuple) -> None:
        kind, slot = message[0], message[1]
        c = self.cams[slot]
        if not c.alive:
            return
        if kind == "started":
            check_id = message[2]
            spec = self.registry.get(check_id)
            c.running = (check_id, time.monotonic() + spec.timeout_s)
            self.on_event(Event("started", slot, check_id, spec.title))
        elif kind == "result":
            _, _, check_id, result, started = message
            self._record(c, check_id, result, started)
            c.running = None
            if check_id in self.sync_holders:
                self.sync_holders[check_id].discard(slot)
                if not self.sync_holders[check_id]:
                    del self.sync_holders[check_id]
                    self._release_resources(f"sync:{check_id}")
        elif kind == "lock":
            self.lock_queue.append((slot, message[2], message[3]))
            self._grant_locks()
        elif kind == "release":
            self._release_resources(slot)
        elif kind == "barrier":
            self.barriers.setdefault(message[2], set()).add(slot)
            self._try_barrier(message[2])
        elif kind == "phase_done":
            c.phase_done = True
            for check_id in list(self.barriers):
                self._try_barrier(check_id)
        elif kind == "lost":
            self._lose(c, f"ошибка стенда: {message[2]}")

    def _record(self, c: _Camera, check_id: str, result: CheckResult, started: str) -> None:
        if check_id in c.done:
            return
        c.done.add(check_id)
        spec = self.registry.get(check_id)
        self.store.add_result(c.slot, spec, result, started)
        if PASSPORT_KEY in result.data:
            self.store.set_passport(c.slot, result.data[PASSPORT_KEY])
        if result.children:
            for child in result.children:
                self.on_event(
                    Event(
                        "result",
                        c.slot,
                        child.check_id,
                        child.title,
                        child.result.status.value,
                        child.result.reason,
                        part=True,
                    )
                )
            self.on_event(Event("done", c.slot, check_id, spec.title))
        else:
            self.on_event(
                Event("result", c.slot, check_id, spec.title, result.status.value, result.reason)
            )

    # --- ресурсы и барьеры --------------------------------------------------------------------

    def _grant_locks(self) -> None:
        for item in list(self.lock_queue):
            slot, check_id, resources = item
            if all(r not in self.locks for r in resources):
                for r in resources:
                    self.locks[r] = slot
                self.lock_queue.remove(item)
                self.cams[slot].inbox.put(("grant", check_id))

    def _release_resources(self, owner) -> None:
        for r in [r for r, o in self.locks.items() if o == owner]:
            del self.locks[r]
        self._grant_locks()
        for check_id in list(self.barriers):  # синхронная проверка могла ждать этот ресурс
            self._try_barrier(check_id)

    def _participants(self, check_id: str) -> set[int]:
        return {
            c.slot
            for c in self._alive()
            if any(cid == check_id and skip is None for cid, skip in c.plan)
            and check_id not in c.done
        }

    def _try_barrier(self, check_id: str) -> None:
        waiting = self.barriers.get(check_id, set())
        needed = self._participants(check_id)
        if not waiting or not needed <= waiting:
            return
        resources = sorted(self.registry.get(check_id).resources)
        if any(r in self.locks for r in resources):
            return  # ресурс ещё занят поочерёдной проверкой — подождём
        owner = f"sync:{check_id}"
        for r in resources:
            self.locks[r] = owner
        self.sync_holders[check_id] = set(waiting)
        del self.barriers[check_id]
        for slot in waiting:
            self.cams[slot].inbox.put(("go", check_id))

    def _abort_waiting(self) -> None:
        for slot, check_id, _ in self.lock_queue:
            self.cams[slot].inbox.put(("abort", check_id))
        self.lock_queue.clear()
        for check_id, waiting in self.barriers.items():
            for slot in waiting:
                self.cams[slot].inbox.put(("abort", check_id))
        self.barriers.clear()

    # --- здоровье рабочих ---------------------------------------------------------------------

    def _alive(self) -> list[_Camera]:
        return [c for c in self.cams.values() if c.alive]

    def _compensate_freeze(self, last_tick: float) -> float:
        """Если координатор сам долго не получал процессорного времени (перегрузка компьютера,
        подкачка), он не может судить о зависании проверок: сроки сдвигаются на время простоя.
        Без этого одна заминка стенда превращала бы все идущие проверки в «зависшие»."""
        now = time.monotonic()
        gap = now - last_tick
        if gap > FREEZE_GAP_S:
            for c in self._alive():
                if c.running:
                    c.running = (c.running[0], c.running[1] + gap)
        return now

    def _check_health(self) -> None:
        now = time.monotonic()
        for c in self._alive():
            if c.running and now > c.running[1]:
                check_id = c.running[0]
                timeout = self.registry.get(check_id).timeout_s
                self._record(
                    c,
                    check_id,
                    CheckResult.stand_error(f"проверка зависла (> {timeout:g} с)"),
                    started="",
                )
                self._lose(c, "ошибка стенда: проверка зависла, камера снята с прогона")
            elif not c.process.is_alive():
                code = c.process.exitcode
                hint = " — убит системой, возможно, не хватило памяти" if code == -9 else ""
                self._lose(
                    c,
                    f"ошибка стенда: рабочий процесс камеры завершился аварийно (код {code}{hint})",
                )

    def _lose(self, c: _Camera, reason: str) -> None:
        if c.process.is_alive():
            c.process.terminate()
        c.alive = False
        self._fill_missing(c, CheckResult.stand_error(reason.removeprefix("ошибка стенда: ")))
        self.on_event(Event("lost", c.slot, detail=reason))
        self._release_resources(c.slot)
        for check_id in list(self.sync_holders):
            self.sync_holders[check_id].discard(c.slot)
            if not self.sync_holders[check_id]:
                del self.sync_holders[check_id]
                self._release_resources(f"sync:{check_id}")
        self.lock_queue = [item for item in self.lock_queue if item[0] != c.slot]
        for check_id in list(self.barriers):
            self.barriers[check_id].discard(c.slot)
            self._try_barrier(check_id)

    def _fill_missing(self, c: _Camera, result: CheckResult) -> None:
        for check_id, _ in c.plan:
            if check_id not in c.done:
                self._record(
                    c, check_id, CheckResult(result.status, reason=result.reason), started=""
                )

    def _shutdown(self) -> None:
        for c in self.cams.values():
            if c.alive:
                c.inbox.put(("quit",))
        for c in self.cams.values():
            c.process.join(timeout=5)
            if c.process.is_alive():
                c.process.terminate()


def summary_status(store: RunStore) -> Status | None:
    """Худший итог прогона: ошибка стенда > не прошла > прошла."""
    statuses = {row["status"] for row in store.results()}
    for status in (Status.STAND_ERROR, Status.FAILED, Status.PASSED):
        if status.value in statuses:
            return status
    return None
