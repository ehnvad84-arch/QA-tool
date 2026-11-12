"""Вход HTTP родного API (api-discovery, T2): Digest MD5, одна попытка, общий признак отказа,
Basic только по HTTPS, предел размера ответа, табу до и после заголовка входа."""

import hashlib
import re

import httpx
import pytest

from qa_tool.checks.rtsp_refusal import refused_password, remember_native_refusal
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place
from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.digest import LoginUnsupported
from qa_tool.drivers.camera.http import HttpLogin, LoginRefused, ResponseTooLarge

URL = "http://127.0.0.1:9114/ISAPI/System/deviceInfo"
REALM, NONCE = "IP Camera", "abc123"
SECRET = "test-secret-1"  # выдуманный: настоящие пароли стенда в репозиторий не попадают


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class Camera401:
    """Камера с Digest: проверяет ответ по паролю, считает отказы, умеет `stale` и Basic."""

    def __init__(self, password=SECRET, stale_once=False, schemes=("digest",), body=b"<ok/>"):
        self.password = password
        self.stale_once = stale_once
        self.schemes = schemes
        self.body = body
        self.nonce = NONCE
        self.requests: list[httpx.Request] = []
        self.refusals = 0

    def challenge(self, stale=False) -> list[tuple[str, str]]:
        out = []
        if "digest" in self.schemes:
            extra = ", stale=TRUE" if stale else ""
            value = f'Digest realm="{REALM}", qop="auth", nonce="{self.nonce}"{extra}'
            out.append(("WWW-Authenticate", value))
        if "basic" in self.schemes:
            out.append(("WWW-Authenticate", f'Basic realm="{REALM}"'))
        return out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        header = request.headers.get("authorization", "")
        if not header:
            return httpx.Response(401, headers=self.challenge())
        if header.startswith("Basic"):
            return httpx.Response(200, content=self.body)
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
        if self.stale_once:
            self.stale_once = False
            self.nonce = "fresh456"
            return httpx.Response(401, headers=self.challenge(stale=True))
        ha1 = _md5(f"{fields['username']}:{REALM}:{self.password}")
        ha2 = _md5(f"{request.method}:{fields['uri']}")
        expected = _md5(f"{ha1}:{fields['nonce']}:{fields['nc']}:{fields['cnonce']}:auth:{ha2}")
        if fields["nonce"] != self.nonce or fields["response"] != expected:
            self.refusals += 1
            return httpx.Response(401, headers=self.challenge())
        return httpx.Response(200, content=self.body)


@pytest.fixture
def camera(monkeypatch):
    def install(**kwargs) -> Camera401:
        cam = Camera401(**kwargs)
        monkeypatch.setattr(http, "_TRANSPORT", httpx.MockTransport(cam))
        return cam

    return install


def context() -> CheckContext:
    target = Camera(slot=7, address="127.0.0.1", port=9114, username="admin")
    return CheckContext(target, SECRET, Place.COLOR_BOX)


def login_for(ctx: CheckContext, password: str) -> HttpLogin:
    """Так свяжет NativeReader (T5): признак отказа — общий `rtsp_refusal`."""
    return HttpLogin(
        "admin",
        password,
        already_refused=refused_password(ctx),
        on_refused=lambda reason: remember_native_refusal(ctx, reason),
    )


def test_right_password_reads_and_reuses_challenge(camera):
    cam = camera()
    login = HttpLogin("admin", SECRET)
    assert http.request("GET", URL, login=login).content == b"<ok/>"
    assert http.request("GET", URL, login=login).status_code == 200
    # первый запрос без заголовка, дальше — подписанные, без лишнего 401
    assert [bool(r.headers.get("authorization")) for r in cam.requests] == [False, True, True]
    assert "nc=00000002" in cam.requests[-1].headers["authorization"]
    assert SECRET not in repr(login)


def test_wrong_password_one_attempt_and_shared_refusal(camera):
    cam = camera()
    ctx = context()
    first = login_for(ctx, "wrong-secret")
    with pytest.raises(LoginRefused):
        http.request("GET", URL, login=first)
    assert cam.refusals == 1
    assert "родного API" in refused_password(ctx)
    with pytest.raises(LoginRefused):
        http.request("GET", URL, login=first)
    second = login_for(ctx, SECRET)  # другой клиент того же прогона
    sent = len(cam.requests)
    with pytest.raises(LoginRefused):
        http.request("GET", URL, login=second)
    assert len(cam.requests) == sent and cam.refusals == 1  # ни запроса, ни второй попытки


def test_password_refused_by_rtsp_sends_nothing(camera):
    cam = camera()
    ctx = context()
    ctx.extra["rtsp_password_refused"] = "камера не приняла логин и пароль для видео"
    with pytest.raises(LoginRefused):
        http.request("GET", URL, login=login_for(ctx, SECRET))
    assert cam.requests == []


def test_stale_nonce_is_one_repeat_not_a_refusal(camera):
    cam = camera(stale_once=True)
    login = HttpLogin("admin", SECRET)
    assert http.request("GET", URL, login=login).status_code == 200
    assert cam.refusals == 0 and not login.refused


def test_basic_over_plain_http_is_not_sent(camera):
    cam = camera(schemes=("basic",))
    with pytest.raises(LoginUnsupported, match="открытым текстом"):
        http.request("GET", URL, login=HttpLogin("admin", SECRET))
    assert all("authorization" not in r.headers for r in cam.requests)


def test_basic_over_https_is_sent(camera):
    camera(schemes=("basic",))
    url = URL.replace("http://", "https://")
    assert http.request("GET", url, login=HttpLogin("admin", SECRET)).status_code == 200


@pytest.mark.parametrize(
    "challenge",
    [
        'Digest realm="x", nonce="n", algorithm=SHA-256',
        'Digest realm="x", nonce="n", qop="auth-int"',
        'Digest realm="x"',
        'Digest realm="' + "r" * 300 + '", nonce="n"',
        "NTLM",
    ],
)
def test_unsupported_or_hostile_challenge_sends_no_password(monkeypatch, challenge):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, headers=[("WWW-Authenticate", challenge)])

    monkeypatch.setattr(http, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(LoginUnsupported):
        http.request("GET", URL, login=HttpLogin("admin", SECRET))
    assert len(seen) == 1 and "authorization" not in seen[0].headers


def test_response_over_limit_is_cut(camera):
    camera(body=b"x" * 5000)
    with pytest.raises(ResponseTooLarge):
        http.request("GET", URL, login=HttpLogin("admin", SECRET), max_bytes=1000)


def test_taboo_is_checked_before_any_request(camera):
    cam = camera()
    with pytest.raises(TabooViolation):
        http.request(
            "PUT",
            "http://127.0.0.1:9114/ISAPI/System/Network/interfaces/1",
            content="<IPAddress/>",
            login=HttpLogin("admin", SECRET),
        )
    assert cam.requests == []
