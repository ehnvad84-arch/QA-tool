import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import NoKeyringError

from qa_tool.core import secrets
from qa_tool.core.models import Camera, ConfigError
from qa_tool.core.secrets import MASK, get_password, redact, set_password

CAMERA = Camera(slot=3, address="10.0.0.3", username="admin")


def test_password_round_trip_through_keyring(memory_keyring):
    set_password(CAMERA, "s3cret")
    assert get_password(CAMERA) == "s3cret"
    assert ("qa-tool", "10.0.0.3:80") in memory_keyring.store


def test_env_variable_wins_for_ci(monkeypatch):
    set_password(CAMERA, "from-keyring")
    monkeypatch.setenv("QA_TOOL_PASSWORD_3", "from-env")
    assert get_password(CAMERA) == "from-env"


def test_missing_password_is_config_error():
    with pytest.raises(ConfigError, match="Место 3: не задан пароль"):
        get_password(CAMERA)


HTTP_EXCHANGE = """POST /api/auth/login HTTP/1.1
Host: 10.0.0.3
Authorization: Digest username="admin", response="6629fae49393a05397450978507c4ef1"
Cookie: session=abc123

{"username": "admin", "password": "s3cret"}
HTTP/1.1 200 OK
Set-Cookie: session=xyz789; HttpOnly
{"token": "eyJhbGciOi"}
GET /cgi?user=admin&password=s3cret&x=1
"""

SOAP_EXCHANGE = """<s:Header><wsse:Security><wsse:UsernameToken>
<wsse:Username>admin</wsse:Username>
<wsse:Password Type="...#PasswordDigest">tuOSpGlFlIXsozq4HFNeeGeFLEI=</wsse:Password>
<wsse:Nonce>LKqI6G/AikKCQrN0zqZFlg==</wsse:Nonce>
</wsse:UsernameToken></wsse:Security></s:Header>"""


@pytest.mark.parametrize(
    "secret",
    ["6629fae49393a05397450978507c4ef1", "abc123", "xyz789", "s3cret", "eyJhbGciOi"],
)
def test_http_secrets_are_redacted(secret):
    assert secret not in redact(HTTP_EXCHANGE)


def test_soap_password_digest_is_redacted():
    cleaned = redact(SOAP_EXCHANGE)
    assert "tuOSpGlFlIXsozq4HFNeeGeFLEI=" not in cleaned
    assert f">{MASK}</wsse:Password>" in cleaned
    assert "<wsse:Username>admin</wsse:Username>" in cleaned


def test_known_secret_redacted_anywhere():
    assert (
        redact("лог: пароль s3cret принят", known_secrets=["s3cret"])
        == f"лог: пароль {MASK} принят"
    )


def test_old_address_only_key_still_read(memory_keyring):
    memory_keyring.store[("qa-tool", "10.0.0.3")] = "old"
    assert get_password(CAMERA) == "old"


def test_two_cameras_behind_one_address_keep_own_passwords():
    first = Camera(slot=1, address="10.0.0.9", username="admin", port=8001)
    second = Camera(slot=2, address="10.0.0.9", username="admin", port=8002)
    set_password(first, "one")
    set_password(second, "two")
    assert (get_password(first), get_password(second)) == ("one", "two")


@pytest.mark.parametrize(
    "text",
    [
        "GET /x?access_token=abc123",
        "rtsp://admin:abc123@10.0.0.1/stream",
        '{"pwd": "abc123"}',
        "login?user=admin&pwd=abc123",
        "sessionid=abc123; path=/",
    ],
)
def test_more_secret_forms_redacted(text):
    assert "abc123" not in redact(text)


class NoStorage(KeyringBackend):
    """Как Linux без Secret Service: любое обращение к хранилищу падает."""

    priority = 1

    def get_password(self, service, username):
        raise NoKeyringError("нет хранилища")

    def set_password(self, service, username, password):
        raise NoKeyringError("нет хранилища")

    def delete_password(self, service, username):
        raise NoKeyringError("нет хранилища")


@pytest.fixture
def no_storage(memory_keyring):
    keyring.set_keyring(NoStorage())
    yield
    secrets._session.clear()
    keyring.set_keyring(memory_keyring)


def test_without_storage_password_lives_in_memory_only(no_storage, tmp_path):
    camera = Camera(slot=1, address="10.0.0.5", username="admin")
    assert set_password(camera, "s3cret") is False
    assert get_password(camera) == "s3cret"


def test_without_storage_and_password_is_config_error(no_storage):
    with pytest.raises(ConfigError, match="добавьте заново"):
        get_password(Camera(slot=1, address="10.0.0.5", username="admin"))


def test_storage_is_preferred_when_it_works(memory_keyring):
    camera = Camera(slot=1, address="10.0.0.5", username="admin")
    assert set_password(camera, "s3cret") is True
    assert not secrets._session
