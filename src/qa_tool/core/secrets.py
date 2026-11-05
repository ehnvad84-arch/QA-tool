"""Пароли камер и очистка доказательств от секретов (docs/rules/security.md)."""

import os
import re

import keyring

from qa_tool.core.models import Camera, ConfigError

SERVICE = "qa-tool"
_session: dict[str, str] = {}  # пароли без системного хранилища: только в памяти этого запуска
MASK = "***"


def _env_name(camera: Camera) -> str:
    return f"QA_TOOL_PASSWORD_{camera.slot}"


def _key(camera: Camera) -> str:
    """Две камеры за одним адресом (NAT, разные порты) не делят пароль."""
    return f"{camera.address}:{camera.port}"


def set_password(camera: Camera, password: str) -> bool:
    """Сохранить пароль камеры по ключу «адрес:порт».

    Файлом хранилище не заменяется (security.md): если системного хранилища нет (Linux без
    Secret Service), пароль остаётся только в памяти до закрытия программы.

    Returns:
        True — пароль в системном хранилище; False — только в памяти этого запуска.
    """
    try:
        keyring.set_password(SERVICE, _key(camera), password)
    except Exception:  # у каждого хранилища и D-Bus свои исключения, итог для стенда один
        _session[_key(camera)] = password
        return False
    _session.pop(_key(camera), None)
    return True


def _stored(camera: Camera) -> str | None:
    try:
        return keyring.get_password(SERVICE, _key(camera)) or keyring.get_password(
            SERVICE,
            camera.address,  # ключ ранних версий — только адрес
        )
    except Exception:  # нет хранилища — значит, и пароля в нём нет
        return None


def storage_name() -> str:
    """Какое хранилище паролей видит программа (для `qa-tool doctor`)."""
    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    return "нет: пароли только в памяти до закрытия" if "fail" in name else name


def get_password(camera: Camera) -> str:
    """Пароль камеры: переменная окружения (CI), память этого запуска, системное хранилище.

    Raises:
        ConfigError: пароль не задан нигде.
    """
    password = os.environ.get(_env_name(camera)) or _session.get(_key(camera)) or _stored(camera)
    if password is None:
        raise ConfigError(
            f"Место {camera.slot}: не задан пароль камеры {camera.address} — уберите камеру с "
            "места и добавьте заново с паролем"
        )
    return password


_PATTERNS = [
    # HTTP-заголовки целиком до конца строки
    (
        re.compile(
            r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-session-id)"
            r"\s*:\s*).+$"
        ),
        rf"\g<1>{MASK}",
    ),
    # WS-Security UsernameToken: <wsse:Password ...>…</wsse:Password>
    (
        re.compile(r"(?s)(<(?:\w+:)?Password\b[^>]*>).*?(</(?:\w+:)?Password>)"),
        rf"\g<1>{MASK}\g<2>",
    ),
    # JSON: "password": "…", "token": "…"
    (
        re.compile(
            r'(?i)("(?:password|passwd|pwd|pass|token|access_token|refresh_token|session|'
            r'session_id|sessionid|secret|auth)"\s*:\s*")[^"]*(")'
        ),
        rf"\g<1>{MASK}\g<2>",
    ),
    # строка запроса и формы: password=…&
    (
        re.compile(
            r"(?i)((?:^|[?&;\s_-])(?:password|passwd|pwd|pass|token|access_token|sessionid|"
            r"session_id|auth|secret)=)[^&\s\"']+"
        ),
        rf"\g<1>{MASK}",
    ),
    # логин и пароль в адресе: rtsp://user:pass@host
    # до последнего @: в самом пароле он тоже бывает
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/@\s]+:)\S+(@)"), rf"\g<1>{MASK}\g<2>"),
]


def redact(text: str, known_secrets: list[str] | None = None) -> str:
    """Вырезать секреты из текста перед записью в журнал, базу или протокол.

    Args:
        text: обмен с камерой, причина, лог.
        known_secrets: пароли, известные вызывающему, — заменяются везде.

    Returns:
        Текст, где пароли, токены, Authorization, UsernameToken и cookie — `***`.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in known_secrets or []:
        if secret:
            text = text.replace(secret, MASK)
    return text
