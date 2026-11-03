import pytest

from qa_tool.core.models import Camera, ConfigError
from qa_tool.core.secrets import MASK, get_password, redact, set_password

CAMERA = Camera(slot=3, address="10.0.0.3", username="admin")


def test_password_round_trip_through_keyring(memory_keyring):
    set_password(CAMERA, "s3cret")
    assert get_password(CAMERA) == "s3cret"
    assert ("qa-tool", "10.0.0.3") in memory_keyring.store


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
