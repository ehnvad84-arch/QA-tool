"""WEB-WALK-1 целиком: вход, обход, строки протокола, отсутствие записи на камере."""

import contextlib
import tempfile
from concurrent.futures import ThreadPoolExecutor

from qa_tool.checks import registry
from qa_tool.checks.web import walk as walk_module
from qa_tool.checks.web.walk import CHECK_ID, SUMMARY_ID, TIMEOUT_MARGIN_S, walk_seconds
from qa_tool.core.checks import CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.store import RunStore
from qa_tool.core.tempfiles import TEMP_PREFIX
from qa_tool.drivers.camera.browser import CameraSilent
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


def threshold(value):
    return Threshold(value, "", "тест")


def walk_with(pages, thresholds=None, stop_requested=None, **options):
    with MockWebCamera(pages, **options) as camera:
        ctx = context(int(camera.url.rsplit(":", 1)[1].strip("/")))
        ctx.thresholds = thresholds or {}
        if stop_requested:
            ctx.stop_requested = stop_requested
        result = run_check(SPEC, ctx)
    return camera, result


def summary_of(result):
    return next(row.result for row in result.children if row.check_id == SUMMARY_ID)


def test_page_limit_names_pages_left_unchecked():
    pages = {"/": HOME, "/image": IMAGE, "/net": NET}
    _, result = walk_with(pages, {"web.pages_limit": threshold(1)}, auth="form")
    assert summary_of(result).status is Status.PASSED, summary_of(result).reason
    assert "не проверены страницы: Изображение" in summary_of(result).actual


def test_element_limit_names_elements_left_unchecked():
    pages = {"/": HOME, "/image": IMAGE, "/net": NET}
    _, result = walk_with(pages, {"web.elements_limit": threshold(2)}, auth="form")
    assert "Перезагрузка" in summary_of(result).actual


def test_time_limit_ends_walk_and_names_the_rest():
    pages = {"/": HOME, "/image": IMAGE, "/net": NET}
    camera, result = walk_with(pages, {"web.walk_limit_min": threshold(0)}, auth="form")
    assert summary_of(result).status is Status.STAND_ERROR  # нечем доказать «прошла»
    assert "web.walk_limit_min" in summary_of(result).actual
    assert camera.sessions == 0  # вход завершён и при истёкшем пределе


def test_limit_above_start_value_does_not_outlive_the_process():
    ctx = context(80)
    ctx.thresholds = {"web.walk_limit_min": threshold(500)}
    assert walk_seconds(ctx) + TIMEOUT_MARGIN_S <= SPEC.timeout_s


def test_camera_silent_during_walk_is_a_failure(monkeypatch):
    real = walk_module.open_page

    def silent(ctx, session, page):
        if page.label == "Изображение":
            raise CameraSilent("страница не загрузилась за 15 с")
        return real(ctx, session, page)

    monkeypatch.setattr(walk_module, "open_page", silent)
    _, result = walk_with({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    row = summary_of(result)
    assert row.status is Status.FAILED
    assert "камера перестала отвечать" in row.reason
    assert len(result.children) > 1  # пройденное остаётся в протоколе
    assert "Изображение" in row.actual


def test_camera_silent_while_looking_for_pages_is_a_failure(monkeypatch):
    def silent(*_):
        raise CameraSilent("страница не загрузилась за 15 с")

    monkeypatch.setattr(walk_module, "discover", silent)
    _, result = walk_with({"/": HOME}, auth="form")
    assert summary_of(result).status is Status.FAILED


def test_stop_is_not_passed_and_ends_the_login():
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 60

    camera, result = walk_with({"/": HOME, "/image": IMAGE, "/net": NET}, None, stop, auth="form")
    assert summary_of(result).status is Status.SKIPPED
    assert camera.sessions == 0


def test_browser_files_are_removed_after_stop(tmp_path, monkeypatch):
    # своя временная папка: в общей /tmp свои qa-web-* создают параллельные прогоны других веток
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    walk_with({"/": HOME}, None, lambda: True, auth="form")
    assert not list(tmp_path.glob(f"{TEMP_PREFIX}*"))


def test_eight_cameras_at_once_do_not_share_anything():
    with contextlib.ExitStack() as stack:
        cameras = [
            stack.enter_context(MockWebCamera({"/": HOME, "/image": IMAGE}, auth="form"))
            for _ in range(8)
        ]

        def one(camera):
            port = int(camera.url.rsplit(":", 1)[1].strip("/"))
            return run_check(SPEC, context(port))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, cameras))
    assert [r.status for r in results] == [Status.PASSED] * 8
    state = [(c.login_attempts, c.sessions) for c in cameras]
    assert state == [(1, 0)] * 8, state


def stored_run(tmp_path, result, port):
    camera = Camera(slot=1, address="127.0.0.1", port=port, username="admin")
    stand = Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=[camera])
    store = RunStore.create(stand, Scenario())
    store.add_result(1, SPEC, result, "t")
    return store


def test_no_password_or_cookie_in_run_folder(tmp_path):
    leaky = """<h1>Камера</h1><a href="/image?token=leaked-token">Изображение</a>
<script>console.error('cookie ' + document.cookie + ' password secret')</script>
<button onclick="throw new Error('secret ' + document.cookie)">Обновить</button>
<button>Пустая</button>"""
    with MockWebCamera({"/": leaky, "/image": IMAGE}, auth="form") as camera:
        port = int(camera.url.rsplit(":", 1)[1].strip("/"))
        result = run_check(SPEC, context(port))
    store = stored_run(tmp_path, result, port)
    folder = store.run_dir
    store.close()
    found = [
        path.name
        for path in folder.rglob("*")
        if path.is_file()
        and any(mark in path.read_bytes() for mark in (b"secret", b"leaked-token", b"sid=ok"))
    ]
    assert not found


def test_silent_camera_and_summary_reach_the_protocol(tmp_path, monkeypatch):
    real = walk_module.open_page

    def silent(ctx, session, page):
        if page.label == "Изображение":
            raise CameraSilent("страница не загрузилась за 15 с")
        return real(ctx, session, page)

    monkeypatch.setattr(walk_module, "open_page", silent)
    with MockWebCamera({"/": HOME, "/image": IMAGE}, auth="form") as camera:
        port = int(camera.url.rsplit(":", 1)[1].strip("/"))
        result = run_check(SPEC, context(port))
    store = stored_run(tmp_path, result, port)
    rows = {r["check_id"]: r for r in store.results(slot=1)}
    assert rows[SUMMARY_ID]["status"] == Status.FAILED.value
    assert "камера перестала отвечать" in rows[SUMMARY_ID]["reason"]
    assert "страниц найдено" in rows[SUMMARY_ID]["actual"]


def test_login_form_is_never_photographed():
    _, result = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    pictures = [e for row in result.children for e in row.result.evidence if e.kind == "image"]
    assert all(b"secret" not in e.content for e in pictures)
    assert not any("вход" in row.title.lower() for row in result.children)


def test_summary_counts_pages_presses_refusals_and_writes():
    _, result = walk({"/": HOME, "/image": IMAGE, "/net": NET}, auth="form")
    actual = summary_of(result).actual
    assert "не нажато: по запрету 2, опасных 1" in actual  # «Обновить сеть», «Выйти»
    assert "записей сделано 0, возвращено 0" in actual
