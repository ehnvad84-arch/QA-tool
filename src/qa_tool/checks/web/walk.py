"""WEB-WALK-1: обход веб-интерфейса любой камеры без записи.

Родитель — «прошла», если обход состоялся: итог лежит в строках страниц и элементов. Не удавшийся
вход и сбой браузера — «ошибка стенда», а не дефект камеры. Настройки не меняются: запись появится
отдельно и только с галочкой.
"""

import time

from qa_tool.checks.web.discover import Discovery, Item, Mark, Page, discover, plan_elements
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
MAX_LISTED = 20  # столько названий помещается в строку сводки
STOP_LOGOUT_S = 3.0  # из 5 с на закрытие (S20): остаток — на сам браузер


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
    """Обход одной камеры: строки протокола и то, что осталось непроверенным."""

    def __init__(self, ctx: CheckContext, session: BrowserSession, deadline: float) -> None:
        self.ctx = ctx
        self.session = session
        self.deadline = deadline
        self.rows: list[ChildResult] = []
        self.pages_opened = 0
        self.pressed = 0
        self.repeated = 0
        self.left_elements: list[str] = []

    def out_of_time(self) -> bool:
        """Истёк ли предел обхода."""
        return time.monotonic() >= self.deadline

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
        self.repeated += plan.repeated
        self.left_elements += plan.over_limit
        address = self.session.activity()[3]
        for item in plan.items:
            if _is_page_link(item):
                continue
            if self.out_of_time():
                self.left_elements.append(item.element.label)
                continue
            self._add("ELEMENT", press(self.ctx, self.session, item))
            self.pressed += item.mark is Mark.ORDINARY
            if self.session.activity()[3] != address:
                open_page(self.ctx, self.session, page)

    def summary(self, found: Discovery, left_pages: list[str], note: str) -> str:
        """Сводка для протокола: при срабатывании предела перечисляет остаток (S19)."""
        parts = [
            f"страниц найдено {len(found.pages) + len(found.left)}, открыто {self.pages_opened}",
            f"элементов нажато {self.pressed}",
        ]
        if left_pages:
            parts.append(f"не проверены страницы: {_listed(left_pages)}")
        if self.left_elements:
            parts.append(f"не проверены элементы: {_listed(self.left_elements)}")
        if self.repeated:
            parts.append(f"одинаковых элементов проверено выборочно, пропущено {self.repeated}")
        if note:
            parts.append(note)
        return "; ".join(parts)


def _listed(names: list[str]) -> str:
    """Названия для сводки: длинный список обрезается, полный нужен только в журнале."""
    shown = ", ".join(names[:MAX_LISTED])
    extra = len(names) - MAX_LISTED
    return f"{shown} и ещё {extra}" if extra > 0 else shown


def walk_seconds(ctx: CheckContext) -> float:
    """Предел обхода, с. Больше стартового нельзя: от него считается срок процесса проверки."""
    start = DEFAULTS["web.walk_limit_min"].value
    return min(limit(ctx, "web.walk_limit_min"), start) * SECONDS_IN_MINUTE


def _finish_after_stop(session: BrowserSession, address: str) -> str:
    """Завершить вход после «Стоп»: браузер должен закрыться быстро, поэтому предел короткий."""
    session.finishing()
    try:
        return logout(session, address, STOP_LOGOUT_S)
    except (BrowserCrashed, CameraSilent):
        return "выйти не удалось: подключение освободится по тайм-ауту камеры"


def _run(ctx: CheckContext, session: BrowserSession, address: str, deadline: float) -> CheckResult:
    entry = login(ctx, session, address)
    if not entry.walk_allowed:
        reason = f"{entry.entry.value}: {entry.reason}" if entry.reason else entry.entry.value
        return CheckResult.stand_error(reason)
    found = Discovery()
    walk = _Walk(ctx, session, deadline)
    note, status, reason = "", Status.PASSED, ""
    try:
        found = discover(ctx, session, address, deadline)
        for page in found.pages:
            if walk.out_of_time():
                break
            walk.page(page)
    except CameraSilent as e:
        status, reason = Status.FAILED, f"камера перестала отвечать: {e}"
    except Stopped:
        status, reason = Status.SKIPPED, "обход остановлен: нажат «Стоп»"
        note = _finish_after_stop(session, address)
    else:
        if walk.out_of_time():
            note = "обход прерван пределом web.walk_limit_min"
        logged_out = logout(session, address)
        note = "; ".join(part for part in (note, logged_out) if part)
        if not walk.pages_opened:
            # доказательства нет: ни одна страница не открыта (never.md)
            status, reason = Status.STAND_ERROR, "обход не открыл ни одной страницы"
    left_pages = [page.label for page in found.pages[walk.pages_opened :]] + found.left
    actual = walk.summary(found, left_pages, note)
    return CheckResult(
        status,
        reason=reason,
        actual=actual,
        evidence=[Evidence("web-walk.txt", actual)],
        children=walk.rows,
    )


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
        «Прошла» со строками страниц и элементов; камера замолчала — «не прошла»; порты закрыты
        или нажат «Стоп» — «пропущена»; вход или браузер не удались — «ошибка стенда».
    """
    deadline = time.monotonic() + walk_seconds(ctx)
    address = web_address(ctx)
    if address is None:
        return CheckResult.skipped("нет веб-интерфейса: порты закрыты")
    try:
        with BrowserSession(
            address, frozenset({ctx.camera.port}), ctx.stop_requested, str(ctx.camera.slot)
        ) as session:
            return _run(ctx, session, address, deadline)
    except (BrowserMissing, BrowserCrashed, CameraSilent, Stopped) as e:
        return CheckResult.stand_error(f"обход не состоялся: {e}")
