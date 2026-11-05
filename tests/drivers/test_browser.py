"""Браузер веб-обхода: запись не доходит до камеры, чтение доходит."""

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
