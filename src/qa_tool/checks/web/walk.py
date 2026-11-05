"""WEB-WALK-1: обход веб-интерфейса любой камеры без записи.

Родитель — «прошла», если обход состоялся: итог лежит в строках страниц и элементов. Не удавшийся
вход и сбой браузера — «ошибка стенда», а не дефект камеры. Настройки не меняются: запись появится
отдельно и только с галочкой.
"""

import time

from qa_tool.checks.web.discover import Item, Mark, Page, discover, plan_elements
from qa_tool.checks.web.limits import DEFAULTS, limit
from qa_tool.checks.web.login import login, logout, web_address
from qa_tool.checks.web.probe import Row, open_page, press
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Status,
)
from qa_tool.drivers.camera.browser import (
    BrowserCrashed,
    BrowserMissing,
    BrowserSession,
    CameraSilent,
    Stopped,
)

CHECK_ID = "WEB-WALK-1"
SECONDS_IN_MINUTE = 60
TIMEOUT_MARGIN_S = 120.0  # вход, выход и закрытие браузера сверх предела обхода


def _child(prefix: str, number: int, row: Row) -> ChildResult:
    check_id = f"WEB-{prefix}-{number}"
    if row.result.status is Status.PASSED and not row.result.evidence:
        # «прошла» без доказательства не засчитывается: здесь доказательство — замер и отклик
        row.result.evidence = [Evidence(f"{check_id}.txt", row.result.actual)]
    return ChildResult(check_id, row.subject, row.result, severity=row.severity)


def _is_page_link(item: Item) -> bool:
    """Ссылка сама стала строкой страницы: нажимать её незачем."""
    return bool(item.element.href) and item.mark is Mark.ORDINARY


class _Walk:
    """Обход одной камеры: строки протокола и счётчики сводки."""

    def __init__(self, ctx: CheckContext, session: BrowserSession) -> None:
        self.ctx = ctx
        self.session = session
        self.rows: list[ChildResult] = []
        self.pages_opened = 0
        self.pressed = 0
        self.not_checked = 0
        self.deadline = time.monotonic() + limit(ctx, "web.walk_limit_min") * SECONDS_IN_MINUTE

    def _add(self, prefix: str, row: Row) -> None:
        self.rows.append(_child(prefix, len(self.rows) + 1, row))

    def page(self, page: Page) -> None:
        """Открыть страницу и пройти её элементы."""
        row = open_page(self.ctx, self.session, page)
        self.pages_opened += 1
        self._add("PAGE", row)
        if row.result.status is Status.SKIPPED:
            return
        plan = plan_elements(self.ctx, row.elements, page.forbidden)
        self.not_checked += plan.repeated + plan.over_limit
        address = self.session.activity()[3]
        for item in plan.items:
            if _is_page_link(item):
                continue
            if time.monotonic() > self.deadline:
                self.not_checked += 1
                continue
            self._add("ELEMENT", press(self.ctx, self.session, item))
            self.pressed += item.mark is Mark.ORDINARY
            if self.session.activity()[3] != address:
                open_page(self.ctx, self.session, page)

    def summary(self, found_pages: int, left_out: int, note: str) -> str:
        """Сводка для протокола."""
        parts = [
            f"страниц найдено {found_pages + left_out}, открыто {self.pages_opened}",
            f"элементов нажато {self.pressed}",
        ]
        if left_out or self.not_checked:
            parts.append(f"не проверено: страниц {left_out}, элементов {self.not_checked}")
        if note:
            parts.append(note)
        return "; ".join(parts)


def _run(ctx: CheckContext, session: BrowserSession, address: str) -> CheckResult:
    entry = login(ctx, session, address)
    if not entry.walk_allowed:
        reason = f"{entry.entry.value}: {entry.reason}" if entry.reason else entry.entry.value
        return CheckResult.stand_error(reason)
    found = discover(ctx, session, address)
    walk = _Walk(ctx, session)
    note = ""
    try:
        for page in found.pages:
            walk.page(page)
    except (CameraSilent, Stopped) as e:
        note = f"обход прерван: {e}"
    if not note:
        note = logout(session, address)
    actual = walk.summary(len(found.pages), found.left_out, note)
    evidence = [Evidence("web-walk.txt", actual)]
    return CheckResult(Status.PASSED, actual=actual, evidence=evidence, children=walk.rows)


@REGISTRY.check(
    CHECK_ID,
    module="web",
    title="Веб-интерфейс",
    description="Программа входит в веб-интерфейс, сама находит страницы и вкладки, открывает "
    "каждую и нажимает доступные элементы. Изменяющие запросы до камеры не доходят. Страницы "
    "сети, учётных записей и обновления только открываются, опасные кнопки не нажимаются.",
    phase=Phase.NETWORK,
    order=900,
    timeout_s=DEFAULTS["web.walk_limit_min"].value * SECONDS_IN_MINUTE + TIMEOUT_MARGIN_S,
    group="Веб-интерфейс",
    help_id="web",
)
def web_walk(ctx: CheckContext) -> CheckResult:
    """Обойти веб-интерфейс камеры.

    Args:
        ctx: контекст проверки.

    Returns:
        «Прошла» со строками страниц и элементов; порты закрыты — «пропущена»; вход или браузер
        не удались — «ошибка стенда».
    """
    address = web_address(ctx)
    if address is None:
        return CheckResult.skipped("нет веб-интерфейса: порты закрыты")
    try:
        with BrowserSession(
            address, frozenset({ctx.camera.port}), ctx.stop_requested, str(ctx.camera.slot)
        ) as session:
            return _run(ctx, session, address)
    except (BrowserMissing, BrowserCrashed, CameraSilent, Stopped) as e:
        return CheckResult.stand_error(f"обход не состоялся: {e}")
