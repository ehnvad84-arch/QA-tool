"""Общий проход изменяющей проверки по ONVIF: журнал записи, журнал прошлого прогона, обрыв на
пропавшей камере и строка возврата (AD-3, never.md).

Им пользуются «Картинка» (этап 7, `checks/image/engine.py`) и «Кодеки» (этап 8); у каждой
свой вид журнала (`core/write_journal.Journal`) и своя папка, поэтому журнал «Картинки» не
закрывается проходом «Кодеков» и наоборот. Что именно пишется и как читается значение — дело
проверки: сюда она передаёт готовые строки «было» и «записывали» и функции возврата.

Порядок для проверки: `settle_old` до первой записи, `note` перед каждой записью и перед
возвратом, `close` после сверки возврата; при пропаже камеры — `lost = True`, перед общим
возвратом `camera_back`.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from qa_tool.core.checks import CheckContext, CheckResult, ChildResult, Evidence, Severity, Status
from qa_tool.core.taboo import TabooViolation
from qa_tool.core.worker import camera_alive
from qa_tool.core.write_journal import (
    RUN_ID_KEY,
    UNREADABLE,
    Journal,
    PendingWrite,
    clear,
    load,
    lost_write_result,
    save,
)

LOST = "камера перестала отвечать во время проверки (сбой камеры или прошивки)"
INTERRUPTED = "проверка прервана"
STOPPED = "прогон остановлен"
POLL_S = 5.0  # как часто спрашивать пропавшую камеру, вернулась ли она


class Abort(RuntimeError):
    """Дальше эту камеру не проверить: камера пропала, отвергла пароль или поток.

    `current` — итог строки, на которой случилось; остальные получают «ошибка стенда» с `rest`.
    """

    def __init__(self, current: CheckResult, rest: str):
        super().__init__(current.reason)
        self.current = current
        self.rest = rest


def lost_abort(error: Exception, expected: str = "") -> Abort:
    """Обрыв «камера перестала отвечать»: текущая строка «не прошла», остальные — ошибка стенда."""
    failed = CheckResult(Status.FAILED, reason=LOST, actual=str(error), expected=expected)
    return Abort(failed, f"{INTERRUPTED}: {LOST}")


class Report(Protocol):
    """Итог возврата драйвера: что не вернулось (имя, было, стало) и прочие расхождения."""

    left: list[tuple[str, str, str]]
    other: list[str]


class WritePass:
    """Журнал и возврат одной изменяющей проверки по одной камере.

    Строки, которые проход добавляет сам (журнал прошлого прогона), копятся в `rows`; проверка
    переносит их в свой результат. Не потокобезопасен: один проход — один процесс камеры.

    Args:
        ctx: контекст проверки; папка журнала — `ctx.extra[journal.folder_key]`, нет — журнал
            не ведётся (проверка без планировщика).
        journal: вид журнала этой проверки.
    """

    def __init__(self, ctx: CheckContext, journal: Journal):
        self.ctx = ctx
        self.journal = journal
        folder = ctx.extra.get(journal.folder_key)
        self.folder = None if folder is None else Path(folder)
        self.rows: list[ChildResult] = []
        self.lost = False  # камера пропадала: перед общим возвратом ждём её

    # --- журнал этого прогона ------------------------------------------------------------------

    def note(
        self,
        page: str,
        label: str,
        kind: str,
        old: str,
        new: str,
        stage: str,
        restore: dict[str, str],
    ) -> None:
        """Записать журнал перед изменением или возвратом.

        Args:
            page: служба ONVIF, куда пишем.
            label: название настройки для протокола.
            kind: ключ настройки, по нему следующий прогон читает текущее значение.
            old: исходное значение, как его покажет протокол.
            new: записываемое значение в том же виде.
            stage: стадия из `write_journal.STAGES`.
            restore: чем вернуть (токен и исходный XML).

        Raises:
            OSError: журнал не записан — менять настройку нельзя.
        """
        if self.folder is None:
            return
        camera = self.ctx.camera
        entry = PendingWrite(
            self.ctx.extra.get(RUN_ID_KEY, ""),
            camera.slot,
            camera.address,
            camera.port,
            camera.vendor,
            page,
            label,
            kind,
            old,
            new,
            stage,
            restore=restore,
        )
        save(self.folder, entry)

    def close(self) -> None:
        """Закрыть журнал: возврат сверен."""
        if self.folder is not None:
            clear(self.folder, self.ctx.camera.address, self.ctx.camera.port)

    # --- журнал прошлого прогона ---------------------------------------------------------------

    def settle_old(
        self,
        now_of: Callable[[PendingWrite], str],
        put_back: Callable[[PendingWrite], str],
        refused: tuple[type[Exception], ...] = (),
    ) -> None:
        """Прошлый прогон оставил незакрытую запись: вернуть её, если значение всё ещё наше,
        иначе — строка «блокер» (AD-3). Без галочки «изменяющие» вернуть нельзя — тоже блокер.

        Args:
            now_of: текущее значение настройки `entry.kind` в виде `entry.old`.
            put_back: вернуть `entry.restore` и прочитать значение заново (тот же вид).
            refused: исключения `put_back`, означающие «возврат не отправлен».

        Raises:
            CameraUnreachable, OnvifFault: камера не ответила — решает проверка.
        """
        if self.folder is None:
            return
        camera = self.ctx.camera
        entry = load(self.folder, camera.address, camera.port)
        if entry is None or entry.run_id == self.ctx.extra.get(RUN_ID_KEY, ""):
            return
        note = self._settle_note(entry, now_of, put_back, refused)
        if not note:
            return
        lost = lost_write_result(entry, self.journal.where)
        lost.reason = f"{lost.reason}; журнал прошлого прогона: {note}"
        spec = self.journal.lost_spec
        self.rows.append(ChildResult(spec.id, spec.title, lost, spec.description, Severity.BLOCKER))
        self.close()  # блокер записан в протокол; следующий прогон его не повторит

    def _settle_note(
        self,
        entry: PendingWrite,
        now_of: Callable[[PendingWrite], str],
        put_back: Callable[[PendingWrite], str],
        refused: tuple[type[Exception], ...],
    ) -> str:
        """Вернуть запись из журнала; пусто — вернулась (строка «прошла» уже добавлена)."""
        if not self.ctx.allow_writes:
            return "галочки «изменяющие» нет — вернуть нельзя, проверьте настройку вручную"
        if entry.stage == UNREADABLE or not entry.restore.get("xml"):
            return "журнал повреждён — вернуть нечем, проверьте настройку вручную"
        now = now_of(entry)
        if now == entry.old:
            self._old_row(entry, f"значение «{entry.label}» уже исходное ({now})")
            return ""
        if now != entry.new:
            return (
                f"значение уже не наше: было {entry.old}, записывали {entry.new}, сейчас {now} — "
                "его поменял кто-то другой, проверьте вручную"
            )
        try:
            back = put_back(entry)
        except (*refused, TabooViolation, ValueError) as e:
            return f"возврат не отправлен: {e}"
        if back != entry.old:
            return f"возврат не подтвердился: ждали {entry.old}, читается {back}"
        self._old_row(entry, f"«{entry.label}» возвращена: {entry.new} → {back}")
        return ""

    def _old_row(self, entry: PendingWrite, text: str) -> None:
        result = CheckResult(
            Status.PASSED,
            reason=f"журнал прошлого прогона закрыт: {text}",
            expected="настройка, оставленная прошлым прогоном, возвращена",
            evidence=[
                Evidence("journal.txt", f"{entry.label}: было {entry.old}, записывали {entry.new}")
            ],
        )
        spec = self.journal.lost_spec
        self.rows.append(ChildResult(spec.id, spec.title, result, spec.description))
        self.close()

    # --- пропавшая камера ----------------------------------------------------------------------

    def camera_back(self, wait_s: float) -> str:
        """Перед общим возвратом дождаться камеры, если она пропадала.

        Args:
            wait_s: сколько ждать, с (порог `*.reconnect_wait_s`: камера могла уйти в перезагрузку).

        Returns:
            Пусто — можно возвращать; иначе причина для строки возврата, журнал остаётся.
        """
        if not self.lost or self._wait_for_camera(wait_s):
            return ""
        return f"камера не вернулась за {wait_s:g} с — журнал записи сохранён"

    def _wait_for_camera(self, wait_s: float) -> bool:
        deadline = time.monotonic() + wait_s
        while True:
            if camera_alive(self.ctx.camera):
                return True
            if time.monotonic() >= deadline or self.ctx.stop_requested():
                return False
            time.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))


def restore_row(
    check_id: str,
    title: str,
    touched: list[str],
    report: Report | None,
    problem: str,
    nothing_changed: str,
) -> ChildResult:
    """Строка «возврат настроек» (блокер): вернулось ли всё, что проверка меняла.

    Args:
        check_id: идентификатор строки, например `IMAGE-RESTORE-1`.
        title: название строки.
        touched: что менялось, в порядке записи; пусто — строка «пропущена».
        report: итог возврата драйвера; None — возврат не выполнен.
        problem: причина, почему возврата не было или ничего не менялось.
        nothing_changed: причина «пропущена», когда ничего не менялось и `problem` пуст.
    """
    expected = "настройки возвращены и сверены"
    if not touched:
        result = CheckResult.skipped(problem or nothing_changed)
    elif report is None:
        result = CheckResult(
            Status.FAILED, reason=problem or "возврат не выполнен", expected=expected
        )
    elif report.left:
        left = "; ".join(f"«{name}»: было {was}, осталось {now}" for name, was, now in report.left)
        result = CheckResult(
            Status.FAILED,
            reason=f"настройки не вернулись: {left}",
            expected=expected,
            evidence=[Evidence("restore.txt", "\n".join(report.other) or left)],
        )
    else:
        text = "\n".join(report.other) or "все изменённые настройки вернулись"
        result = CheckResult(
            Status.PASSED,
            reason=f"возвращены и сверены: {', '.join(touched)}",
            expected=expected,
            evidence=[Evidence("restore.txt", text)],
        )
    description = "Возвращаем настройки, как были до проверки, и сверяем."
    return ChildResult(check_id, title, result, description, Severity.BLOCKER)


def total(rows: list[ChildResult]) -> CheckResult:
    """Составной итог: «не прошла», если не прошла хоть одна строка."""
    bad = any(r.result.status is Status.FAILED for r in rows)
    status = Status.FAILED if bad else Status.PASSED
    return CheckResult(status, reason=f"строк: {len(rows)}", children=rows)
