import pytest

from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.ruver import client as client_module
from qa_tool.drivers.camera.ruver.client import (
    CallRefused,
    LoginRejected,
    NoFreeSession,
    Operation,
    RuverClient,
    load_map,
    password_hash,
)
from tests.mock_ruver import MockRuver

API = load_map()
BRIGHTNESS = API.get("POST", "/api/image/brightness")
INFO = API.get("GET", "/api/info")
REBOOT = API.get("POST", "/api/service/reboot")


@pytest.fixture
def camera():
    mock = MockRuver().start()
    yield mock
    mock.stop()


def client_for(camera, password="admin", **options):
    return RuverClient("127.0.0.1", camera.port, "admin", password, **options)


def test_password_is_md5_with_session():
    assert password_hash("admin", "s") == "9aeb4bf9dd880748397812ac97cf929c"


def test_session_opens_and_closes(camera):
    with client_for(camera) as client:
        assert client.call(INFO).status_code == 200
        assert len(camera.sessions) == 1
    assert camera.sessions == {}
    assert len(camera.closed_sessions) == 1


def test_password_does_not_travel_in_clear(camera):
    camera.password = "s3cret-word"
    with client_for(camera, password="s3cret-word"):
        pass
    assert "s3cret-word" not in str(camera.requests)


def test_session_closes_on_failure(camera):
    with pytest.raises(RuntimeError), client_for(camera):
        raise RuntimeError
    assert camera.sessions == {}


def test_wrong_password_is_one_attempt_and_frees_session(camera):
    with pytest.raises(LoginRejected), client_for(camera, password="wrong"):
        pass
    assert camera.login_attempts == 1
    assert camera.sessions == {}


def test_no_free_session(camera):
    camera.busy_sessions = 3
    with pytest.raises(NoFreeSession), client_for(camera):
        pass
    assert camera.login_attempts == 0


def test_expired_session_is_reopened_once(camera):
    camera.expire_session_after = 1
    with client_for(camera) as client:
        assert client.call(INFO).status_code == 200
    assert camera.login_attempts == 2
    assert camera.sessions == {}


def test_second_expiry_is_not_reopened(camera):
    with client_for(camera) as client:
        client._relogin_used = True
        camera.sessions.clear()
        assert client.call(INFO).status_code == 401
    assert camera.login_attempts == 1


def test_write_without_permission_is_refused_before_network(camera):
    with client_for(camera) as client, pytest.raises(CallRefused):
        client.call(BRIGHTNESS, {"brightness": 1})
    assert not camera.sent("POST", BRIGHTNESS.path)


def test_destructive_without_permission_is_refused_before_network(camera):
    with client_for(camera, allow_writes=True) as client, pytest.raises(CallRefused):
        client.call(REBOOT)
    assert not camera.sent("POST", REBOOT.path)


def test_operation_outside_the_map_is_refused(camera, monkeypatch):
    sent = []
    with client_for(camera, allow_writes=True) as client:
        monkeypatch.setattr(http, "request", lambda *a, **k: sent.append(a))
        foreign = Operation("X", "GET", "/api/new/secret", "?", "чтение")
        with pytest.raises(CallRefused):
            client.call(foreign)
        monkeypatch.undo()
    assert not sent


def test_taboo_kind_is_refused_even_with_all_permissions(camera):
    taboo = API.get("POST", "/api/network/tcpip")
    options = {"allow_writes": True, "allow_destructive": True}
    with client_for(camera, **options) as client, pytest.raises(CallRefused):
        client.call(taboo)
    assert not camera.sent("POST", taboo.path)


def test_forged_operation_is_stopped_by_taboo_too(camera, monkeypatch):
    forged = Operation("X", "POST", "/api/users/set_password", "?", "запись")
    with client_for(camera, allow_writes=True) as client:
        monkeypatch.setattr(client, "_check_allowed", lambda op: None)
        with pytest.raises(TabooViolation):
            client.call(forged, {"password": "x"})
    assert not camera.sent("POST", forged.path)


def test_map_file_is_in_the_package():
    assert client_module.MAP_FILE.exists()
