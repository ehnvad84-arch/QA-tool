"""Пароли камер и очистка доказательств от секретов (docs/rules/security.md)."""

import os
import re

import keyring

from qa_tool.core.models import Camera, ConfigError

SERVICE = "qa-tool"
MASK = "***"


def _env_name(camera: Camera) -> str:
    return f"QA_TOOL_PASSWORD_{camera.slot}"


def set_password(camera: Camera, password: str) -> None:
    keyring.set_password(SERVICE, camera.address, password)


def get_password(camera: Camera) -> str:
    """Сначала переменная окружения (CI), потом системное хранилище паролей."""
    password = os.environ.get(_env_name(camera)) or keyring.get_password(SERVICE, camera.address)
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
        re.compile(r'(?i)("(?:password|passwd|token|session|session_id)"\s*:\s*")[^"]*(")'),
        rf"\g<1>{MASK}\g<2>",
    ),
    # строка запроса и формы: password=…&
    (re.compile(r"(?i)\b((?:password|passwd|token)=)[^&\s\"']+"), rf"\g<1>{MASK}"),
]


def redact(text: str, known_secrets: list[str] | None = None) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in known_secrets or []:
        if secret:
            text = text.replace(secret, MASK)
    return text
