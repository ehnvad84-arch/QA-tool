"""Фиктивные проверки для тестов планировщика: пишут отметки времени в trace_dir."""

import json
import time
from pathlib import Path

from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.checks import CheckResult, Evidence, Phase, Registry, Severity, Status
from qa_tool.core.web_journal import FOLDER_KEY, RUN_ID_KEY, PendingWrite, save

REGISTRY = Registry()


def _traced(seconds: float):
    def check(ctx):
        start = time.time()
        time.sleep(seconds)
        end = time.time()
        trace = Path(ctx.extra["trace_dir"]) / f"{ctx.camera.slot}.jsonl"
        with trace.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": ctx.extra.get("_id"), "start": start, "end": end}) + "\n")
        return CheckResult(Status.PASSED, evidence=[Evidence("trace.txt", f"{start}-{end}")])

    return check


def _register(check_id: str, seconds: float, **options):
    func = _traced(seconds)

    def wrapped(ctx):
        ctx.extra["_id"] = check_id
        return func(ctx)

    REGISTRY.check(check_id, "fake", check_id, "фиктивная проверка", **options)(wrapped)


_register("FAKE-NET-1", 0.05)
_register("FAKE-NET-2", 0.05)
_register("FAKE-SYNC-1", 0.1, phase=Phase.SYNC, resources={"light"})
_register("FAKE-LIGHT-1", 0.1, phase=Phase.SEQUENTIAL, resources={"light"})
_register("FAKE-STRESS-1", 0.05, phase=Phase.STRESS)
_register("FAKE-SLOW-1", 1.0)
_register("FAKE-SLOW-2", 1.0)
_register("FAKE-SLOW-3", 1.0)
_register("FAKE-HANG-1", 30, timeout_s=1)


@REGISTRY.check(
    "FAKE-WRITE-HANG",
    module="web",
    title="Запись и зависание",
    description="",
    phase=Phase.NETWORK,
    timeout_s=1,
)
def write_and_hang(ctx):
    """Записать журнал и зависнуть: планировщик снимет процесс посреди записи."""
    entry = PendingWrite(
        ctx.extra[RUN_ID_KEY],
        ctx.camera.slot,
        ctx.camera.address,
        ctx.camera.port,
        "generic",
        "http://admin:x@cam/image?token=abc#/video",
        "Яркость",
        "range",
        "50",
        "60",
        "sent",
    )
    save(Path(ctx.extra[FOLDER_KEY]), entry)
    time.sleep(30)
    return CheckResult.skipped("сюда процесс не дойдёт")


def _fails_after(seconds: float):
    def check(ctx):
        time.sleep(seconds)
        return CheckResult(Status.FAILED, reason="камера ответила ошибкой")

    return check


# после первой проверки (order 200): к этому моменту камера уже «отвечала»
REGISTRY.check("FAKE-FAILS-1", "fake", "падает", "", order=200)(_fails_after(1.0))
# три синхронные проверки на одном свете; средняя — только для производителя «b» (находка W9)
_register("FAKE-SYNC-X", 0.1, phase=Phase.SYNC, resources={"light"}, order=1)
_register("FAKE-SYNC-Y", 0.1, phase=Phase.SYNC, resources={"light"}, order=2, vendors={"b"})
_register("FAKE-SYNC-Z", 0.1, phase=Phase.SYNC, resources={"light"}, order=3)

MAIN = ["FAKE-NET-1", "FAKE-NET-2", "FAKE-SYNC-1", "FAKE-LIGHT-1", "FAKE-STRESS-1"]
SLOW = ["FAKE-SLOW-1", "FAKE-SLOW-2", "FAKE-SLOW-3"]


def _composite(ctx):
    from qa_tool.core.checks import ChildResult

    ok = CheckResult(Status.PASSED, evidence=[Evidence("x.txt", "Authorization: Digest s3cret")])
    return CheckResult(
        Status.PASSED,
        children=[
            ChildResult("SUB-1", "первая", ok),
            ChildResult(
                "SUB-2",
                "вторая",
                CheckResult(Status.FAILED, reason="password=s3cret отказ"),
                severity=Severity.BLOCKER,
            ),
            ChildResult("SUB-3", "без доказательства", CheckResult(Status.PASSED)),
        ],
    )


REGISTRY.check("FAKE-COMPOSITE-1", "fake", "составная", "что делаем")(_composite)


def _composite_camera_vanishes(ctx):
    """Составная проверка, во время которой камера пропадает (её останавливает тест)."""
    from qa_tool.core.checks import ChildResult

    ctx.progress("Контраст · 50 % · 2 из 9 · password=s3cret")
    time.sleep(1.0)
    ok = CheckResult(Status.PASSED, evidence=[Evidence("x.txt", "кадр")])
    return CheckResult(
        Status.PASSED,
        children=[
            ChildResult("SUB-A", "первая", ok),
            ChildResult("SUB-B", "вторая", ok),
            ChildResult("SUB-C", "третья", CheckResult(Status.STAND_ERROR, reason="нет ответа")),
        ],
    )


REGISTRY.check("FAKE-COMPOSITE-LOST", "fake", "составная, камера пропадает", "")(
    _composite_camera_vanishes
)


def _passport(ctx):
    passport = {"manufacturer": "Фейк", "model": "<b>FK-1</b>", "firmware": "1.0"}
    return CheckResult(
        Status.PASSED,
        evidence=[Evidence("passport.txt", "ok")],
        data={PASSPORT_KEY: passport},
    )


# первой (order 1), как CORE-CONNECT-1: паспорт нужен окну до остальных результатов
REGISTRY.check("FAKE-PASSPORT-1", "fake", "паспорт", "", order=1)(_passport)
