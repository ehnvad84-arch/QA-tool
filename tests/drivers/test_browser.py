"""Браузер веб-обхода: запись не доходит до камеры, чтение доходит."""

import contextlib
import time
from pathlib import Path

import pytest

from qa_tool.drivers.camera.browser import BrowserSession, Stopped, Verdict, is_camera_url
from tests.mock_web import MockWebCamera

SEND_ON_LOAD = """<script>
fetch('/api/image/brightness', {method: 'POST', body: '{"value": 5}'}).catch(() => {});
fetch('/status.json').catch(() => {});
</script>"""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://10.0.0.1/index.html", True),
        ("http://10.0.0.1:8080/a", True),
        ("http://10.0.0.2/", False),
        ("http://10.0.0.1:9999/", False),
        ("http://10.0.0.1.evil.test/", False),
        ("http://evil.test/?h=10.0.0.1", False),
        ("data:text/html,x", True),
    ],
)
def test_camera_url(url, expected):
    assert is_camera_url(url, "10.0.0.1", frozenset({80, 8080})) is expected


def test_write_does_not_reach_camera_and_read_does():
    with MockWebCamera({"/": SEND_ON_LOAD, "/status.json": "{}"}) as camera:
        with BrowserSession(camera.url) as session:
            assert session.open(camera.url + "/").status == 200
            session.wait(300)
            stopped = list(session.stopped)
        assert camera.writes() == []
        assert ("GET", "/status.json") in camera.arrived
    assert [(s.method, s.verdict) for s in stopped] == [("POST", Verdict.STOP_WRITE)]


def test_foreign_host_is_stopped():
    page = {"/": '<img src="http://foreign.test/x.png">'}
    with MockWebCamera(page) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        stopped = list(session.stopped)
    assert [s.verdict for s in stopped] == [Verdict.STOP_FOREIGN]


def test_session_hides_browser_objects():
    public = {name for name in vars(BrowserSession) if not name.startswith("_")}
    assert not public & {"page", "context", "request", "route", "browser"}


def test_close_twice_is_safe():
    with MockWebCamera({"/": "x"}) as camera:
        session = BrowserSession(camera.url)
        with session:
            pass
        session.close()


def fire(path, method="POST", body="{}"):
    """Страница, которая при загрузке шлёт один запрос."""
    options = f"{{method: '{method}', body: '{body}'}}" if method != "GET" else "{}"
    return f"<script>fetch('{path}', {options})</script>"


def visit(page, window=None, **kwargs):
    """Открыть страницу в нужном окне; вернуть камеру и остановленные запросы."""
    with MockWebCamera({"/": page}, **kwargs) as camera, BrowserSession(camera.url) as session:
        with window(session) if window else contextlib.nullcontext():
            session.open(camera.url + "/")
            session.wait(300)
        return camera, list(session.stopped)


def test_login_page_reads_approved_by_director_reach_camera():
    # «Рувер» .100 и Hikvision .112 шлют эти запросы при загрузке страницы входа, ещё до окна
    # входа; без ответа форма не работает (решение директора 11.11)
    page = fire("/api/auth/create_session", "GET", "") + fire("/SDK/activateStatus?_=1", "GET", "")
    camera, stopped = visit(page)
    assert ("GET", "/api/auth/create_session") in camera.arrived
    assert ("GET", "/SDK/activateStatus?_=1") in camera.arrived
    assert stopped == []


def test_taboo_stops_even_in_write_window():
    camera, stopped = visit(fire("/api/users/delete"), lambda s: s.write_window())
    assert camera.writes() == []
    assert stopped[0].verdict is Verdict.STOP_TABOO


def test_danger_stops_even_in_write_window():
    camera, stopped = visit(fire("/api/service/reboot"), lambda s: s.write_window())
    assert camera.writes() == []
    assert stopped[0].verdict is Verdict.STOP_DANGER


def test_danger_get_is_stopped():
    camera, stopped = visit(fire("/cgi-bin/reboot.cgi?now=1", "GET", ""))
    assert camera.arrived == [("GET", "/")]
    assert stopped[0].verdict is Verdict.STOP_DANGER


def test_write_window_sends_one_save():
    camera, stopped = visit(fire("/api/image/brightness"), lambda s: s.write_window())
    assert camera.writes() == [("POST", "/api/image/brightness")]
    assert stopped == []


def test_write_window_closes_on_first_answer():
    page = """<script>
    fetch('/api/image/a', {method: 'POST', body: '1'}).then(
      () => fetch('/api/image/b', {method: 'POST', body: '2'}));
    </script>"""
    camera, stopped = visit(page, lambda s: s.write_window())
    assert camera.writes() == [("POST", "/api/image/a")]
    assert [s.verdict for s in stopped] == [Verdict.STOP_WRITE]


def test_redirect_of_write_to_taboo_does_not_reach_camera():
    target = "/api/users/set_password"
    camera, stopped = visit(
        fire("/api/image/save"),
        lambda s: s.write_window(),
        redirects={"/api/image/save": (307, target)},
    )
    assert ("POST", target) not in camera.arrived
    assert stopped[0].verdict is Verdict.STOP_TABOO


def test_form_redirect_of_write_is_followed_by_new_request():
    page = '<form id="f" method="post" action="/save"></form><script>f.submit()</script>'
    pages = {"/": page, "/done": "сохранено"}
    jump = {"/save": (303, "/done")}
    with (
        MockWebCamera(pages, jump) as camera,
        BrowserSession(camera.url) as session,
        session.write_window(),
    ):
        session.open(camera.url + "/")
        session.wait(500)
    assert ("POST", "/save") in camera.arrived
    assert ("GET", "/done") in camera.arrived


def test_unknown_get_while_pressing_is_stopped():
    page = "<script>setTimeout(() => fetch('/api/lens/focus'), 300)</script>"
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        with session.pressing():
            session.wait(600)
        stopped = list(session.stopped)
    assert ("GET", "/api/lens/focus") not in camera.arrived
    assert any(s.url.endswith("/api/lens/focus") for s in stopped)


def test_known_get_while_pressing_passes():
    pages = {"/": "<script>fetch('/status.json')</script>", "/status.json": "{}"}
    with MockWebCamera(pages) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.wait(300)
        before = camera.arrived.count(("GET", "/status.json"))
        with session.pressing():
            session.open(camera.url + "/")
            session.wait(300)
        assert camera.arrived.count(("GET", "/status.json")) == before + 1


def test_read_redirect_to_forbidden_is_noticed():
    page = '<img src="/jump.png">'
    jump = {"/jump.png": (302, "/cgi-bin/reboot.cgi?now=1")}
    with MockWebCamera({"/": page}, jump) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.wait(300)
        assert session.presses_stopped


def test_login_window_opens_once():
    with MockWebCamera({"/": "x"}) as camera, BrowserSession(camera.url) as session:
        with session.login_window("admin", "secret"):
            pass
        with pytest.raises(RuntimeError):
            session.login_window("admin", "secret")


def test_login_window_lets_login_through_and_stops_change():
    page = fire("/api/auth/login", body='{"username": "admin", "password": "network1"}')
    camera, _ = visit(page, lambda s: s.login_window("admin", "network1"))
    assert camera.writes() == [("POST", "/api/auth/login")]
    change = fire("/api/auth/login", body='{"oldPassword": "a", "newPassword": "b"}')
    camera, stopped = visit(change, lambda s: s.login_window("admin", "a"))
    assert camera.writes() == []
    assert stopped[0].verdict is Verdict.STOP_TABOO


def test_login_window_limits_writes():
    page = "<script>for (let i = 0; i < 6; i++) fetch('/login' + i, {method: 'POST', body: ''})</script>"
    camera, stopped = visit(page, lambda s: s.login_window("admin", "x"))
    assert len(camera.writes()) == 4
    assert len(stopped) == 2


def socket_page(message):
    return f"""<script>
    const socket = new WebSocket(location.origin.replace('http', 'ws') + '/ws');
    socket.onopen = () => socket.send({message!r});
    </script>"""


@pytest.mark.parametrize(
    ("message", "arrives"),
    [
        ('{"ping": 1}', True),
        ('{"cmd": "set", "value": 1}', False),
        ('{"action": "reboot"}', False),
        ('{"network": "dhcp"}', False),
    ],
)
def test_socket_messages(message, arrives):
    camera, _ = visit(socket_page(message))
    assert (message in camera.ws_messages) is arrives


def test_binary_socket_message_is_stopped():
    page = """<script>
    const socket = new WebSocket(location.origin.replace('http', 'ws') + '/ws');
    socket.onopen = () => socket.send(new Uint8Array([1, 2, 3]));
    </script>"""
    camera, _ = visit(page)
    assert camera.ws_messages == []


FORM = """<h2>Изображение</h2><label for="b">Яркость</label><input id="b" name="brightness" value="5">
<input type="password" name="p"><button id="save">Сохранить</button>
<button disabled>Недоступно</button><select name="mode"><option>a</option><option>b</option></select>
<input type="checkbox" name="on"><button style="display:none">Скрыта</button>"""


def by_label(items, label):
    return next(item for item in items if item.label == label)


def test_elements_are_collected_with_labels_and_state():
    with MockWebCamera({"/": FORM}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        items = session.elements()
        assert session.password_fields() == 1
    labels = [item.label for item in items]
    assert "Яркость" in labels
    assert "Скрыта" not in labels
    assert by_label(items, "Недоступно").disabled
    assert not by_label(items, "Сохранить").disabled
    assert "Изображение" in by_label(items, "Яркость").area


def test_fields_read_and_write_in_browser_only():
    with MockWebCamera({"/": FORM}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        items = session.elements()
        field = by_label(items, "Яркость")
        assert session.read_field(field) == "5"
        session.set_field(field, "7")
        assert session.read_field(field) == "7"
        box = next(item for item in items if item.kind == "checkbox")
        session.set_field(box, "true")
        assert session.read_field(box) == "true"
        select = next(item for item in items if item.tag == "select")
        session.set_field(select, "b")
        assert session.read_field(select) == "b"
    assert camera.writes() == []


def test_click_reports_stopped_request():
    page = """<button onclick="fetch('/api/image/save', {method: 'POST', body: '1'})">Сохранить</button>"""
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        reaction = session.click(by_label(session.elements(), "Сохранить"), wait_ms=300)
    assert [s.url.rsplit("/", 1)[1] for s in reaction.stopped] == ["save"]
    assert camera.writes() == []


@pytest.mark.parametrize(
    ("kind", "accepted"),
    [("alert('a')", True), ("confirm('b')", False), ("prompt('c')", False)],
)
def test_dialogs_do_not_hang(kind, accepted):
    page = f"<button onclick=\"const r = {kind}; fetch('/result?' + r)\">Нажать</button>"
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.click(by_label(session.elements(), "Нажать"), wait_ms=300)
        assert session.dialogs
    assert any(path.startswith("/result") for _, path in camera.arrived) or not accepted


def test_confirm_with_danger_word_is_refused_in_write_window():
    page = (
        """<button onclick="fetch('/seen?' + confirm('Перезагрузить камеру?'))">Нажать</button>"""
    )
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        element = by_label(session.elements(), "Нажать")
        with session.write_window():
            session.click(element, wait_ms=300)
    assert ("GET", "/seen?true") not in camera.arrived


def test_stop_works_on_hung_page_and_cleans_up():
    # страница зависает после загрузки: цикл до события load на нагруженном стенде иногда
    # опережал ответ на картинку, и load не наступал вовсе — тест падал не по своей причине
    page = (
        "<script>addEventListener('load', () => setTimeout(() => { while (true) {} }, 50))"
        "</script><img src='/slow.png'>"
    )
    flag = {"stop": False}
    with MockWebCamera({"/": page}) as camera:
        session = BrowserSession(camera.url, stop_requested=lambda: flag["stop"], temp_tag="t")
        with session:
            session.open(camera.url + "/")
            temp = session._temp_dir
            # цикл стартует через 50 мс после load: чтение, начатое раньше, встаёт в очередь
            # за ним, и предел Playwright (он только на поиск элемента) его уже не прерывает —
            # тест висел в CI; ждём, пока страница точно зависнет
            time.sleep(0.5)
            assert session.elements() == []
            flag["stop"] = True
            with pytest.raises(Stopped):
                session.wait(5000)
            started = time.monotonic()
        assert time.monotonic() - started < 5
    assert not Path(temp).exists()


def test_screenshot_hides_password_and_skips_login_and_forbidden_pages():
    with MockWebCamera({"/": FORM}) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        assert session.screenshot().startswith(b"\x89PNG")
        session.screens_allowed = False
        assert session.screenshot() is None
        session.screens_allowed = True
        with session.login_window("admin", "x"):
            assert session.screenshot() is None


def test_popup_is_closed_and_noted():
    page = "<script>window.open('/other')</script>"
    pages = {"/": page, "/other": "x"}
    with MockWebCamera(pages) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.wait(400)
        assert session.popups


def test_sessions_are_isolated():
    page = "<script>document.cookie = 'sid=1'; fetch('/seen?' + document.cookie)</script>"
    with MockWebCamera({"/": page}) as camera:
        with BrowserSession(camera.url) as first:
            first.open(camera.url + "/")
            first.wait(200)
            state = first.session_state()
        assert state["cookies"]
        with BrowserSession(camera.url) as second:
            assert second.session_state()["cookies"] == []


def test_traffic_is_recorded_passively():
    page = """<script>
    fetch('/api/image.json').catch(() => {});
    fetch('/api/osd.xml').catch(() => {});
    fetch('/api/image/brightness', {method: 'POST', body: '{"brightness": 5, "mode": "x"}'})
      .catch(() => {});
    </script>"""
    pages = {
        "/": page,
        "/api/image.json": '{"brightness": 50, "mode": "auto"}',
        "/api/osd.xml": "<Osd><Title>cam</Title></Osd>",
    }
    with MockWebCamera(pages) as camera:
        with BrowserSession(camera.url) as session:
            session.open(camera.url + "/")
            session.wait(300)
            traffic = {(t.method, t.url.split(camera.url)[-1]): t for t in session.traffic}
        arrived = list(camera.arrived)
    image = traffic[("GET", "/api/image.json")]
    assert image.status == 200 and b'"brightness": 50' in image.body
    assert b"<Title>cam</Title>" in traffic[("GET", "/api/osd.xml")].body
    assert traffic[("GET", "/")].body == b""  # разметка страницы — не данные API
    # запись остановлена до камеры: в трафике её нет, своих запросов браузер не делал
    assert ("POST", "/api/image/brightness") not in traffic
    assert all(method == "GET" for method, _ in arrived)
    assert arrived.count(("GET", "/api/image.json")) == 1  # тело взято из ответа странице
