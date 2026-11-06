"""Поиск страниц и элементов веб-интерфейса: что есть и что с этим можно делать.

Решения о подписях и адресах чистые и работают на `ElementInfo`. Обход открывает страницы по ссылкам
и нажимает только вкладки и пункты меню обычных страниц: иначе страницы SPA не найти. Запретная
страница только читается, её признак наследуют страницы, найденные с неё. Записи здесь нет:
`BrowserSession` останавливает изменяющие запросы при любом нажатии.
"""

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urljoin, urlsplit

from qa_tool.checks.web.limits import limit
from qa_tool.core.checks import CheckContext
from qa_tool.core.taboo import web_danger_word, web_forbidden_word
from qa_tool.drivers.camera.browser import BrowserSession, ElementInfo

HOME_LABEL = "Главная"
SETTLE_MS = 500  # интерфейсы дорисовывают страницу после загрузки и после нажатия вкладки
NAVIGATION_ROLES = frozenset({"tab", "menuitem", "treeitem"})
NOT_PAGES = (".pdf", ".zip", ".exe", ".msi", ".bin", ".apk", ".tar", ".gz", ".jpg", ".png")
PLUGIN_WORDS = (
    "plug-in",
    "plugin",
    "плагин",
    "activex",
    "webcomponents",
    "npapi",
    "надстройк",
)


class Mark(Enum):
    """Что программа делает с элементом."""

    ORDINARY = "обычный"
    FORBIDDEN = "запрещён табу"
    DANGER = "есть, не нажат"
    DISABLED = "недоступен"


@dataclass(frozen=True)
class Page:
    """Найденная страница; `tab` — вкладка, которую нужно нажать после открытия адреса."""

    url: str
    label: str
    forbidden: str = ""
    tab: str = ""


@dataclass(frozen=True)
class Item:
    """Элемент страницы с решением и причиной для протокола."""

    element: ElementInfo
    mark: Mark
    reason: str = ""


@dataclass
class ElementPlan:
    """Элементы страницы после выборки и то, что осталось непроверенным."""

    items: list[Item] = field(default_factory=list)
    repeated: int = 0
    over_limit: list[str] = field(default_factory=list)


@dataclass
class Discovery:
    """Итог поиска страниц; `left` — найденные, но не открытые из-за предела."""

    pages: list[Page] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


def classify(element: ElementInfo, page_forbidden: str = "") -> Item:
    """Решить, что делать с элементом.

    Опасное проверяется первым: на запретной странице причина «опасный» точнее, а нажатия нет
    в обоих случаях.

    Args:
        element: элемент страницы.
        page_forbidden: причина запрета страницы; пусто — страница обычная.

    Returns:
        Элемент с решением и причиной.
    """
    texts = (element.label, element.name, element.id, element.href)
    word = web_danger_word(*texts)
    if word:
        return Item(element, Mark.DANGER, f"опасное действие (признак «{word}»)")
    word = web_forbidden_word(*texts)
    if word:
        return Item(element, Mark.FORBIDDEN, f"табу (признак «{word}»)")
    if page_forbidden:
        return Item(element, Mark.FORBIDDEN, f"страница под табу ({page_forbidden})")
    if element.disabled:
        return Item(element, Mark.DISABLED, "элемент недоступен")
    return Item(element, Mark.ORDINARY)


def page_of_link(element: ElementInfo, current: str, parent_forbidden: str = "") -> Page | None:
    """Страница, на которую ведёт ссылка; None — ссылка не на страницу этой камеры.

    Args:
        element: элемент с `href`.
        current: адрес открытой страницы.
        parent_forbidden: причина запрета страницы, где найдена ссылка.

    Returns:
        Страница или None. Опасная ссылка не открывается: открытие адреса может быть действием.
    """
    href = element.href.strip()
    if not href or href == "#" or href.lower().startswith(("javascript:", "mailto:", "tel:")):
        return None
    url = urljoin(current, href)
    parts, own = urlsplit(url), urlsplit(current)
    if parts.netloc != own.netloc or parts.path.lower().endswith(NOT_PAGES):
        return None
    if web_danger_word(element.label, href):
        return None
    word = web_forbidden_word(element.label, href)
    forbidden = f"признак «{word}»" if word else parent_forbidden
    return Page(url, element.label or href, forbidden)


def needs_plugin(text: str) -> str:
    """Признак просьбы поставить плагин браузера; пусто — такой просьбы нет."""
    lowered = text.lower()
    return next((word for word in PLUGIN_WORDS if word in lowered), "")


def _same_key(element: ElementInfo) -> tuple[str, ...]:
    """Ключ похожих элементов: строки таблицы отличаются только номерами."""
    fields = (element.tag, element.kind, element.role, element.label, element.name, element.area)
    return tuple(re.sub(r"\d+", "#", value) for value in fields)


def plan_elements(
    ctx: CheckContext, elements: list[ElementInfo], page_forbidden: str = ""
) -> ElementPlan:
    """Отобрать элементы страницы для проверки.

    Args:
        ctx: контекст проверки; пороги `web.same_elements_limit` и `web.elements_limit`.
        elements: элементы страницы.
        page_forbidden: причина запрета страницы.

    Returns:
        Элементы с решениями и число пропущенных повторов и элементов сверх предела.
    """
    same_limit = int(limit(ctx, "web.same_elements_limit"))
    total_limit = int(limit(ctx, "web.elements_limit"))
    plan = ElementPlan()
    seen: dict[tuple[str, ...], int] = {}
    for element in elements:
        key = _same_key(element)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > same_limit:
            plan.repeated += 1
        elif len(plan.items) >= total_limit:
            plan.over_limit.append(element.label)
        else:
            plan.items.append(classify(element, page_forbidden))
    return plan


def _open(session: BrowserSession, ctx: CheckContext, page: Page) -> list[ElementInfo]:
    """Открыть страницу и дать ей дорисоваться."""
    session.page_label = page.label
    session.open(page.url, limit(ctx, "web.page_load_s"))
    session.wait(SETTLE_MS)
    return session.elements()


def _navigation(elements: list[ElementInfo], forbidden: str) -> list[ElementInfo]:
    """Вкладки и пункты меню без адреса, которые можно нажать ради поиска страниц."""
    if forbidden:
        return []
    return [
        element
        for element in elements
        if element.role in NAVIGATION_ROLES
        and not element.href
        and classify(element).mark is Mark.ORDINARY
    ]


def discover(
    ctx: CheckContext, session: BrowserSession, address: str, deadline: float = math.inf
) -> Discovery:
    """Найти страницы интерфейса от главной.

    Args:
        ctx: контекст проверки; порог `web.pages_limit`.
        session: сессия после входа.
        address: адрес главной страницы.
        deadline: момент `time.monotonic()`, после которого новые страницы не открываются.

    Returns:
        Страницы в порядке открытия и названия не открытых из-за предела.

    Raises:
        CameraSilent: страница не загрузилась.
        Stopped: нажат «Стоп».
        BrowserCrashed: браузер упал.
    """
    pages_limit = int(limit(ctx, "web.pages_limit"))
    found = Discovery()
    queue = deque([Page(address, HOME_LABEL)])
    seen = {address}

    def remember(elements: list[ElementInfo], page: Page) -> None:
        for element in elements:
            target = page_of_link(element, page.url, page.forbidden)
            if target is not None and target.url not in seen:
                seen.add(target.url)
                queue.append(target)

    while queue and len(found.pages) < pages_limit and time.monotonic() < deadline:
        page = queue.popleft()
        elements = _open(session, ctx, page)
        found.pages.append(page)
        remember(elements, page)
        for element in _navigation(elements, page.forbidden):
            key = f"{page.url}\n{element.label}"
            if key in seen or element.label == page.label:
                continue
            seen.add(key)
            with session.pressing():
                reaction = session.click(element, wait_ms=SETTLE_MS)
            if reaction.stopped:
                continue
            if len(found.pages) >= pages_limit:
                found.left.append(element.label)
                continue
            found.pages.append(Page(page.url, element.label, tab=element.label))
            remember(session.elements(), page)
    found.left += [page.label for page in queue]
    return found
