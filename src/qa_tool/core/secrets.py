"""Пароли камер и очистка доказательств от секретов (docs/rules/security.md)."""

import os
import re

import keyring

from qa_tool.core.models import Camera, ConfigError

SERVICE = "qa-tool"
MASK = "***"


def _env_name(camera: Camera) -> str:
    return f"QA_TOOL_PASSWORD_{camera.slot}"


def _key(camera: Camera) -> str:
    """Две камеры за одним адресом (NAT, разные порты) не делят пароль."""
    return f"{camera.address}:{camera.port}"


def set_password(camera: Camera, password: str) -> None:
    """Сохранить пароль камеры в системном хранилище по ключу «адрес:порт»."""
    keyring.set_password(SERVICE, _key(camera), password)


def get_password(camera: Camera) -> str:
    """Сначала переменная окружения (CI), потом системное хранилище паролей."""
    password = (
        os.environ.get(_env_name(camera))
        or keyring.get_password(SERVICE, _key(camera))
        or keyring.get_password(SERVICE, camera.address)  # ключ ранних версий — только адрес
    )
    if password is None:
        raise ConfigError(f"Место {camera.slot}: не задан пароль камеры {camera.address}")
    return password


_PATTERNS = [
    # HTTP-заголовки целиком до конца строки
    (
        re.compile(r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).+$"),
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
            r"(?i)((?:^|[?&;\s_])(?:password|passwd|pwd|pass|token|access_token|sessionid|"
            r"session_id|auth|secret)=)[^&\s\"']+"
        ),
        rf"\g<1>{MASK}",
    ),
    # логин и пароль в адресе: rtsp://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@\s]+(@)"), rf"\g<1>{MASK}\g<2>"),
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
