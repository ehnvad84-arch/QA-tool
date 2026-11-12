"""Ловушки перехвата: страница пытается провести запись мимо браузерного запрета.

Судья — только сервер (`arrived`, `ws_messages`). Если ловушка сработала, запрос до камеры дошёл, и
гарантия «до камеры не дошло» в протоколе была бы ложью.
"""

import pytest

from qa_tool.drivers.camera.browser import BrowserSession
from tests.mock_web import MockWebCamera

POST = "fetch('/api/image/set', {method: 'POST', body: 'x'})"


def run(pages, *, leave=False, wait=500, redirects=None):
    """Открыть «/», подождать, при необходимости уйти на другую страницу; вернуть камеру."""
    pages = {"/other": "другая страница", **pages}
    with MockWebCamera(pages, redirects) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.wait(wait)
        if leave:
            session.open(camera.url + "/other")
            session.wait(wait)
    return camera


def test_send_beacon_on_leave():
    script = "addEventListener('pagehide', () => navigator.sendBeacon('/api/image/set', 'x'))"
    assert run({"/": f"<script>{script}</script>"}, leave=True).writes() == []


def test_keepalive_fetch_on_leave():
    script = (
        "addEventListener('pagehide', () => "
        "fetch('/api/image/set', {method: 'POST', body: 'x', keepalive: true}))"
    )
    assert run({"/": f"<script>{script}</script>"}, leave=True).writes() == []


def test_keepalive_request_object_on_leave():
    script = (
        "addEventListener('pagehide', () => fetch(new Request('/api/image/set', "
        "{method: 'POST', body: 'x', keepalive: true})))"
    )
    assert run({"/": f"<script>{script}</script>"}, leave=True).writes() == []


def test_request_from_detached_iframe():
    script = """
    const frame = document.createElement('iframe');
    document.body.append(frame);
    frame.contentWindow.fetch('/api/image/set', {method: 'POST', body: 'x', keepalive: true});
    frame.remove();
    """
    assert run({"/": f"<body><script>{script}</script></body>"}).writes() == []


def test_service_worker():
    worker = f"self.addEventListener('install', () => {POST}); {POST};"
    script = "navigator.serviceWorker.register('/sw.js').catch(() => {})"
    camera = run({"/": f"<script>{script}</script>", "/sw.js": worker}, wait=800)
    assert camera.writes() == []


def test_shared_worker():
    script = "try { new SharedWorker('/w.js'); } catch (e) {}"
    camera = run({"/": f"<script>{script}</script>", "/w.js": f"{POST};"}, wait=800)
    assert camera.writes() == []


def test_dedicated_worker():
    script = "new Worker('/w.js')"
    camera = run({"/": f"<script>{script}</script>", "/w.js": f"{POST};"}, wait=800)
    assert camera.writes() == []


def test_form_in_popup():
    form = (
        '<form id="f" target="_blank" method="post" action="/api/image/set">'
        '<input name="a" value="1"></form><script>f.submit()</script>'
    )
    assert run({"/": form}, wait=800).writes() == []


def test_window_open_then_form():
    script = """
    const popup = window.open('/other');
    setTimeout(() => {
      const form = popup.document.createElement('form');
      form.method = 'post'; form.action = '/api/image/set';
      popup.document.body.append(form); form.submit();
    }, 200);
    """
    assert run({"/": f"<script>{script}</script>"}, wait=800).writes() == []


def test_redirect_to_foreign_host_goes_nowhere():
    with MockWebCamera({"/x": "чужая"}) as foreign:
        target = foreign.url.replace("127.0.0.1", "localhost") + "/x"
        run({"/": '<img src="/jump.png">'}, redirects={"/jump.png": (302, target)})
    assert foreign.arrived == []


@pytest.mark.parametrize("name", ["RTCPeerConnection", "WebTransport", "SharedWorker"])
def test_side_channels_are_removed(name):
    script = f"fetch('/seen?' + (typeof window.{name}))"
    camera = run({"/": f"<script>{script}</script>"})
    assert ("GET", "/seen?undefined") in camera.arrived


def test_page_socket_message_does_not_reach_camera():
    script = """
    const socket = new WebSocket(location.origin.replace('http', 'ws') + '/ws');
    socket.onopen = () => socket.send('{"cmd": "set"}');
    """
    camera = run({"/": f"<script>{script}</script>"})
    assert camera.ws_messages == []


def test_worker_socket_is_noticed():
    worker = "new WebSocket(self.location.origin.replace('http', 'ws') + '/ws');"
    pages = {"/": "<script>new Worker('/w.js')</script>", "/w.js": worker, "/other": "x"}
    with MockWebCamera(pages) as camera, BrowserSession(camera.url) as session:
        session.open(camera.url + "/")
        session.wait(800)
        assert session.presses_stopped
