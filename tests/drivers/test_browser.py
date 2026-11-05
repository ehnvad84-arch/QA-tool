"""Браузер веб-обхода: запись не доходит до камеры, чтение доходит."""

import contextlib

import pytest

from qa_tool.drivers.camera.browser import BrowserSession, Verdict, is_camera_url
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
            assert session.open(camera.url + "/") == 200
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
    with MockWebCamera(pages, jump) as camera, BrowserSession(camera.url) as session:
        with session.write_window():
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
