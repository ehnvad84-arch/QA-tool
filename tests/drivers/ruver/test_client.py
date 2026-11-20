import pytest

from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.ruver import client as client_module
from qa_tool.drivers.camera.ruver.client import (
    CallRefused,
    LoginFailed,
    LoginRejected,
    NoFreeSession,
    Operation,
    RuverClient,
    challenge_password,
    load_map,
    password_hash,
)
from tests.mock_ruver import MockRuver

API = load_map()
BRIGHTNESS = API.get("POST", "/api/image/brightness")
INFO = API.get("GET", "/api/info")
BRIGHTNESS_READ = API.get("GET", "/api/image/brightness")
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


def test_return_operation_needs_write_permission(camera):
    block = API.get("POST", "/api/osd/set_block_position")
    with client_for(camera) as client, pytest.raises(CallRefused):
        client.call(block, {"id": 1, "x": 1, "y": 1})
    assert not camera.sent("POST", block.path)


def test_expected_restart_allows_one_more_login(camera):
    with client_for(camera) as client:
        client._relogin_used = True
        client.expect_restart()
        camera.sessions.clear()
        camera.expire_session_after = 0
        assert client.call(BRIGHTNESS_READ).status_code == 200
    assert camera.login_attempts == 2


def test_expected_restart_does_not_lift_a_password_refusal(camera):
    with pytest.raises(LoginRejected), client_for(camera, password="wrong") as client:
        pass
    client = client_for(camera, password="wrong")
    with pytest.raises(LoginRejected):
        client._login()
    client.expect_restart()
    assert client._relogin_used is True


def test_expected_restart_does_not_lift_a_lockout(camera, monkeypatch):
    client = client_for(camera)
    locked = http.Response(401, json={"errorCode": 2003})
    created = http.Response(200, json={"sessionID": "s"})
    no_challenge = http.Response(404)
    answers = iter([created, no_challenge, locked])
    monkeypatch.setattr(http, "request", lambda *a, **k: next(answers))
    with pytest.raises(LoginFailed):
        client._login()
    client.expect_restart()
    assert client._relogin_used is True


def test_map_file_is_in_the_package():
    assert client_module.MAP_FILE.exists()


# --- вход PBKDF2 своей сборки прошивки (docs/work/ruver-login) ---------------------------------


def test_challenge_password_matches_firmware_formula():
    # Пример из лабораторного клиента прошивки tools/lab/camd-api.py: те же шаги на Python
    import hashlib
    import hmac

    key = hashlib.pbkdf2_hmac("sha256", b"test-secret-1", bytes.fromhex("00" * 16), 3)
    expected = hmac.new(key, b"sid-1", "sha256").hexdigest()
    assert challenge_password("test-secret-1", "sid-1", "00" * 16, 3) == expected


@pytest.mark.parametrize("mode", ["both", "challenge"])
def test_challenge_camera_is_entered_with_pbkdf2_in_one_attempt(camera, mode):
    camera.login_mode = mode
    with client_for(camera) as client:
        assert client.call(INFO).status_code == 200
        assert client.login_method == "PBKDF2"
    assert camera.login_attempts == 1
    assert len(camera.sent("POST", "/api/auth/challenge")) == 1


def test_factory_camera_without_challenge_is_entered_with_md5(camera):
    with client_for(camera) as client:
        assert client.login_method == "MD5"
    assert camera.login_attempts == 1


def test_wrong_password_on_challenge_camera_is_one_attempt(camera):
    camera.login_mode = "challenge"
    with pytest.raises(LoginRejected), client_for(camera, password="wrong"):
        pass
    assert camera.login_attempts == 1
    assert camera.sessions == {}


def test_challenge_password_does_not_travel_in_clear(camera):
    camera.login_mode = "challenge"
    camera.password = "test-secret-1"
    with client_for(camera, password="test-secret-1"):
        pass
    assert "test-secret-1" not in str(camera.requests)


@pytest.mark.parametrize(
    "answer",
    [
        (200, "<html>login</html>"),
        (200, {"salt": "00" * 16, "iterations": 10000, "algorithm": "md5"}),
        (200, {"salt": "zz" * 16, "iterations": 10000, "algorithm": "pbkdf2-sha256"}),
        (200, {"salt": "00" * 16, "iterations": "10000", "algorithm": "pbkdf2-sha256"}),
        (200, {"salt": "00" * 16, "iterations": True, "algorithm": "pbkdf2-sha256"}),
        (200, {"salt": "00" * 16, "iterations": 0, "algorithm": "pbkdf2-sha256"}),
        (405, None),
    ],
)
def test_strange_challenge_answer_means_md5(camera, answer):
    camera.challenge_answer = answer
    with client_for(camera) as client:
        assert client.login_method == "MD5"
    assert camera.login_attempts == 1


def test_huge_iterations_stop_before_login_attempt(camera):
    camera.challenge_answer = (
        200,
        {"salt": "00" * 16, "iterations": 10**9, "algorithm": "pbkdf2-sha256"},
    )
    with pytest.raises(LoginFailed, match="итераций"), client_for(camera):
        pass
    assert camera.login_attempts == 0
    assert camera.sessions == {}


def test_relogin_reuses_known_login_without_new_challenge(camera):
    camera.login_mode = "challenge"
    camera.expire_session_after = 1
    with client_for(camera) as client:
        assert client.call(INFO).status_code == 200
    assert camera.login_attempts == 2
    assert len(camera.sent("POST", "/api/auth/challenge")) == 1


def test_lockout_429_is_not_retried_after_restart(camera):
    camera.lockout_429 = True
    client = client_for(camera)
    with pytest.raises(LoginFailed, match="заблокировала"):
        client._login()
    client.expect_restart()
    assert client._relogin_used is True
    client.close()
