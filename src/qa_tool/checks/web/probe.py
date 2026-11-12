"""Открытие страницы и нажатие элемента без записи: реакция, ошибки, замеры.

Судим только о том, что видно: изменяющие запросы останавливает `BrowserSession`, поэтому «прошла»
у кнопки значит только, что она откликнулась, а эффект без галочки не проверялся. Часы и счётчики
на странице реакцией не считаются: цифры из текста убираются перед сравнением.
"""

import re
from dataclasses import dataclass, field

from qa_tool.checks.web.discover import Item, Mark, Page, needs_plugin
from qa_tool.checks.web.limits import DEFAULTS, limit
from qa_tool.core.checks import CheckContext, CheckResult, Evidence, Severity, Status
from qa_tool.core.models import Threshold
from qa_tool.drivers.camera.browser import BrowserSession, ElementInfo, StoppedRequest, Verdict

HTTP_ERROR = 400
PAGE_SETTLE_MS = 500
NOT_ACTED_ON = "до камеры не дошло, эффект не проверялся"
FIELD_TAGS = frozenset({"input", "select", "textarea"})
CLICKABLE_KINDS = frozenset({"button", "submit"})


@dataclass
class Row:
    """Строка протокола по странице или элементу."""

    subject: str
    result: CheckResult
    severity: Severity | None = None
    elements: list[ElementInfo] = field(default_factory=list)


def _threshold(ctx: CheckContext, key: str) -> Threshold:
    return ctx.thresholds.get(key, DEFAULTS[key])


def _snapshot(session: BrowserSession) -> list[Evidence]:
    picture = session.screenshot()
    return [Evidence("page.png", picture, kind="image")] if picture else []


def _plain_text(session: BrowserSession) -> str:
    """Текст страницы без цифр: часы и счётчики меняются сами."""
    return re.sub(r"\d+", "", session.page_text())


def _failed(session: BrowserSession, subject: str, actual: str, severity: Severity) -> Row:
    result = CheckResult(Status.FAILED, actual=actual, evidence=_snapshot(session))
    return Row(subject, result, severity)


def open_page(ctx: CheckContext, session: BrowserSession, page: Page) -> Row:
    """Открыть страницу и оценить загрузку.

    Args:
        ctx: контекст проверки; порог `web.page_load_s`.
        session: сессия после входа.
        page: страница из `discover`.

    Returns:
        Строка страницы и её элементы. Плагин — «пропущена», ошибки и задержка — «не прошла».

    Raises:
        CameraSilent: страница не загрузилась.
        Stopped: нажат «Стоп».
        BrowserCrashed: браузер упал.
    """
    errors, answers = len(session.page_errors), len(session.bad_answers)
    session.page_label = page.label
    load = session.open(page.url, limit(ctx, "web.page_load_s") * 3)
    session.wait(PAGE_SETTLE_MS)
    if page.tab:
        tab = next((e for e in session.elements() if e.label == page.tab), None)
        if tab is not None:
            with session.pressing():
                session.click(tab, wait_ms=PAGE_SETTLE_MS)
    elements = session.elements()
    plugin = needs_plugin(session.page_text())
    if plugin:
        reason = f"страница просит плагин браузера (признак «{plugin}»)"
        return Row(page.label, CheckResult.skipped(reason), elements=elements)
    quiet_s = limit(ctx, "web.click_response_s")
    problems = session.errors_since(errors, quiet_s) + session.bad_answers[answers:]
    if load.status is not None and load.status >= HTTP_ERROR:
        problems.insert(0, f"страница ответила кодом {load.status}")
    if problems:
        row = _failed(session, page.label, "; ".join(problems), Severity.MAJOR)
        row.elements = elements
        return row
    allowed = limit(ctx, "web.page_load_s")
    if load.elapsed_s > allowed:
        actual = f"загрузка {load.elapsed_s:.1f} с при пределе {allowed:.1f} с"
        row = _failed(session, page.label, actual, Severity.MINOR)
        row.result.threshold = _threshold(ctx, "web.page_load_s")
        row.elements = elements
        return row
    actual = f"загрузка {load.elapsed_s:.1f} с"
    return Row(page.label, CheckResult(Status.PASSED, actual=actual), elements=elements)


def _stopped_row(subject: str, stopped: list[StoppedRequest]) -> Row | None:
    """Строка по самому строгому из остановленных запросов; None — только обычная запись."""
    addresses = ", ".join(f"{s.method} {s.url}" for s in stopped)
    if any(s.verdict in (Verdict.STOP_TABOO, Verdict.STOP_FOREIGN) for s in stopped):
        return Row(subject, CheckResult.stand_error(f"запрос не отправлен: {addresses}"))
    if any(s.verdict is Verdict.STOP_DANGER for s in stopped):
        return Row(subject, CheckResult.skipped(f"опасное действие, есть, не нажат: {addresses}"))
    return None


def _not_pressed(item: Item) -> str:
    """Причина, по которой элемент не нажимается; пусто — нажимать можно."""
    element = item.element
    if item.mark is not Mark.ORDINARY:
        return item.reason
    if element.tag in FIELD_TAGS and element.kind not in CLICKABLE_KINDS:
        return "поле ввода: без галочки «изменяющие» не меняется"
    return ""


def press(ctx: CheckContext, session: BrowserSession, item: Item) -> Row:
    """Нажать элемент и оценить реакцию.

    Args:
        ctx: контекст проверки; порог `web.click_response_s`.
        session: сессия, страница элемента открыта.
        item: элемент с решением из `classify`.

    Returns:
        Строка элемента. Реакцией считаются запрос, смена адреса, диалог и изменение текста.

    Raises:
        Stopped: нажат «Стоп».
        BrowserCrashed: браузер упал.
    """
    subject = item.element.label or item.element.name or item.element.tag
    reason = _not_pressed(item)
    if reason:
        return Row(subject, CheckResult.skipped(reason))
    before = _plain_text(session)
    allowed = limit(ctx, "web.click_response_s")
    with session.pressing():
        reaction = session.click(item.element, wait_ms=int(allowed * 1000))
    row = _stopped_row(subject, reaction.stopped)
    if row is not None:
        return row
    if reaction.stopped:
        addresses = ", ".join(f"{s.method} {s.url}" for s in reaction.stopped)
        actual = f"{addresses}: {NOT_ACTED_ON}"
        return Row(subject, CheckResult(Status.PASSED, actual=actual))
    if reaction.requests or reaction.navigated or reaction.dialog or _plain_text(session) != before:
        return Row(subject, CheckResult(Status.PASSED, actual="элемент откликнулся"))
    mark = session.activity()
    session.wait(int(allowed * 1000))
    if session.activity() != mark or _plain_text(session) != before:
        row = _failed(session, subject, f"отклик позже {allowed:.1f} с", Severity.MINOR)
        row.result.threshold = _threshold(ctx, "web.click_response_s")
        return row
    return _failed(session, subject, "нет ни запроса, ни изменения на странице", Severity.MINOR)
