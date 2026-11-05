"""WEB-WALK-1 целиком: вход, обход, строки протокола, отсутствие записи на камере."""

from qa_tool.checks import registry
from qa_tool.checks.web.walk import CHECK_ID
from qa_tool.core.checks import CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from tests.mock_web import MockWebCamera

SPEC = registry().get(CHECK_ID)
HOME = """<h1>Камера</h1><nav><a href="/image">Изображение</a><a href="/net">Сеть</a></nav>
<button onclick="fetch('/seen')">Обновить</button>
<button onclick="fetch('/api/image/save', {method: 'POST'})">Сохранить</button>
<button>Пустая</button>
<button onclick="fetch('/reboot-now')">Перезагрузка</button>
<button disabled>Серая</button>
<button onclick="fetch('/api/auth/logout', {method: 'POST'})">Выйти</button>"""
IMAGE = "<h1>Изображение</h1><input aria-label='Яркость'><a href='/'>Назад</a>"
NET = "<h1>Сеть</h1><button onclick=\"fetch('/seen-net')\">Обновить сеть</button>"


def context(port, password="secret"):
    camera = Camera(slot=1, address="127.0.0.1", port=port, username="admin")
    return CheckContext(camera, password, Place.COLOR_BOX)


def walk(pages, **options):
    with MockWebCamera(pages, **options) as camera:
        result = run_check(SPEC, context(int(camera.url.rsplit(":", 1)[1].strip("/"))))
    return camera, result


def by_title(result):
    return {row.title: row for row in result.children}


def test_walk_writes_rows_for_pages_and_elements():
    camera, result = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    assert result.status is Status.PASSED, result.reason
    rows = by_title(result)
    assert rows["Изображение"].result.status is Status.PASSED
    assert rows["Обновить"].result.status is Status.PASSED
    assert rows["Сохранить"].result.status is Status.PASSED
    assert "до камеры не дошло" in rows["Сохранить"].result.actual
    assert rows["Пустая"].result.status is Status.FAILED
    assert rows["Перезагрузка"].result.status is Status.SKIPPED
    assert rows["Серая"].result.status is Status.SKIPPED
    assert rows["Яркость"].result.status is Status.SKIPPED


def test_only_login_and_logout_reach_camera():
    camera, _ = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    assert camera.writes() == [("POST", "/api/auth/login"), ("POST", "/api/auth/logout")]
    assert camera.sessions == 0


def test_danger_and_forbidden_page_elements_are_not_pressed():
    camera, result = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    assert ("GET", "/reboot-now") not in camera.arrived
    assert ("GET", "/seen-net") not in camera.arrived
    assert by_title(result)["Обновить сеть"].result.status is Status.SKIPPED


def test_row_ids_are_unique():
    _, result = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    ids = [row.check_id for row in result.children]
    assert len(ids) == len(set(ids))
    assert all(row.check_id.startswith("WEB-") for row in result.children)


def test_refused_password_is_stand_error_without_rows():
    with MockWebCamera({"/": HOME}, auth="form") as camera:
        ctx = context(int(camera.url.rsplit(":", 1)[1].strip("/")), password="wrong")
        result = run_check(SPEC, ctx)
    assert result.status is Status.STAND_ERROR
    assert result.children == []
    assert camera.login_attempts == 1


def test_closed_ports_are_skipped(monkeypatch):
    monkeypatch.setattr("qa_tool.checks.web.login.tcp_open", lambda *_args: False)
    result = run_check(SPEC, context(1))
    assert result.status is Status.SKIPPED
