"""Поиск страниц и элементов: чистые решения на элементах и обход виртуального интерфейса."""

import pytest

from qa_tool.checks.web.discover import (
    Mark,
    classify,
    discover,
    needs_plugin,
    page_of_link,
    plan_elements,
)
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.browser import BrowserSession, ElementInfo
from tests.mock_web import MockWebCamera

SPA = """<nav><a href="#/image">Изображение</a><a href="#/video">Видео</a>
<a href="#/network">Сеть</a></nav><main id="v"></main><script>
const views = {
  '#/image': '<h2>Изображение</h2><a href="#/image/osd">Надписи</a><a href="#/video">Видео</a>',
  '#/image/osd': '<h2>Надписи</h2><a href="#/image">Назад</a>',
  '#/video': '<h2>Видео</h2><div role="tab" aria-selected="true">Основной</div>' +
    '<div role="tab" onclick="v.innerHTML=\\'<a href=&quot;#/extra&quot;>Запас</a>\\'">Доп</div>',
  '#/extra': '<h2>Запас</h2>',
  '#/network': '<h2>Сеть</h2><a href="#/network/dns">Имена</a><div role="tab">Адреса</div>',
  '#/network/dns': '<h2>Имена</h2>',
};
const show = () => { v.innerHTML = views[location.hash] || '<h2>Главная</h2>'; };
addEventListener('hashchange', show); show();
</script>"""


def element(label="", tag="button", role="", href="", name="", disabled=False, area="", kind=""):
    return ElementInfo(0, f"{tag}:{label}", tag, kind, role, label, name, "", href, area, disabled)


def context(**limits):
    camera = Camera(slot=1, address="127.0.0.1", port=80, username="admin")
    thresholds = {key: Threshold(value, "", "тест") for key, value in limits.items()}
    return CheckContext(camera, "secret", Place.COLOR_BOX, thresholds=thresholds)


def run(pages, **limits):
    with MockWebCamera(pages) as camera, BrowserSession(camera.url) as session:
        found = discover(context(**limits), session, camera.url + "/")
        return camera, found


@pytest.mark.parametrize(
    ("label", "mark"),
    [
        ("Перезагрузка", Mark.DANGER),
        ("Reboot", Mark.DANGER),
        ("Форматировать карту", Mark.DANGER),
        ("Формат даты", Mark.ORDINARY),
        ("Яркость", Mark.ORDINARY),
        ("Пароль", Mark.FORBIDDEN),
        ("Настройки сети", Mark.FORBIDDEN),
    ],
)
def test_labels_are_classified(label, mark):
    assert classify(element(label)).mark is mark


def test_danger_wins_over_forbidden_page():
    item = classify(element("Перезагрузка"), page_forbidden="сеть")
    assert item.mark is Mark.DANGER


def test_nothing_is_pressed_on_forbidden_page():
    item = classify(element("Яркость"), page_forbidden="сеть")
    assert item.mark is Mark.FORBIDDEN
    assert "сеть" in item.reason


def test_disabled_element_is_marked_with_reason():
    item = classify(element("Яркость", disabled=True))
    assert item.mark is Mark.DISABLED
    assert item.reason


def test_address_counts_not_only_label():
    assert classify(element("Открыть", tag="a", href="/cgi-bin/reboot")).mark is Mark.DANGER


def test_foreign_link_is_not_a_page():
    assert page_of_link(element("x", tag="a", href="http://10.0.0.9/"), "http://cam/") is None


@pytest.mark.parametrize("href", ["javascript:void(0)", "mailto:a@b", "#", "", "/a.pdf", "/a.zip"])
def test_links_that_are_not_pages(href):
    assert page_of_link(element("x", tag="a", href=href), "http://cam/") is None


def test_same_elements_are_sampled():
    rows = [element(f"Камера {n}", name=f"row{n}") for n in range(40)]
    plan = plan_elements(context(), rows)
    assert len(plan.items) == 3
    assert plan.repeated == 37


def test_elements_limit_says_what_is_left():
    rows = [element(f"Кнопка {chr(1040 + n)}") for n in range(30)]
    plan = plan_elements(context(**{"web.elements_limit": 10}), rows)
    assert len(plan.items) == 10
    assert len(plan.over_limit) == 20


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please install the plug-in to view", True),
        ("Установите плагин для просмотра", True),
        ("ActiveX control is required", True),
        ("Изображение и видео", False),
    ],
)
def test_plugin_request(text, expected):
    assert bool(needs_plugin(text)) is expected


def test_spa_pages_and_tabs_are_found():
    camera, found = run({"/": SPA})
    names = {page.label for page in found.pages}
    assert {"Изображение", "Надписи", "Видео", "Доп", "Запас", "Сеть", "Имена"} <= names
    assert not found.left
    assert camera.writes() == []


def test_network_pages_are_forbidden_and_inherit_it():
    _, found = run({"/": SPA})
    marks = {page.label: bool(page.forbidden) for page in found.pages}
    assert marks["Сеть"] and marks["Имена"]
    assert not marks["Изображение"]
    assert "Адреса" not in marks  # вкладка запретной страницы не нажимается


def test_link_cycle_ends():
    camera, found = run(
        {"/": '<a href="/a">А</a>', "/a": '<a href="/b">Б</a>', "/b": '<a href="/a">А</a>'}
    )
    assert [page.label for page in found.pages] == ["Главная", "А", "Б"]
    assert camera.writes() == []


def test_endless_pages_stop_at_limit_and_say_so():
    pages = {"/": '<a href="/p1">1</a><a href="/p2">2</a><a href="/p3">3</a><a href="/p4">4</a>'}
    _, found = run(pages, **{"web.pages_limit": 2})
    assert len(found.pages) == 2
    assert len(found.left) == 3


def test_danger_menu_item_is_not_pressed():
    page = "<div role='menuitem' onclick=\"fetch('/seen')\">Перезагрузка</div>"
    camera, _ = run({"/": page})
    assert ("GET", "/seen") not in camera.arrived
