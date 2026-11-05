"""Расчёт HTTP Digest (RFC 2617) для RTSP и браузера веб-обхода.

Вынесен отдельно, потому что `route.fetch` браузера умеет только Basic, а считать должно одно и то
же: неверный ответ стоит попытки на счётчике камеры. Только MD5: другой алгоритм камеры в RTSP
не предлагают, а веб-обход такую камеру пропускает.
"""

import hashlib
import os
import re


def parse_challenge(header: str) -> dict[str, str]:
    """Разобрать `WWW-Authenticate` в схему и параметры."""
    scheme = "basic" if header.lower().startswith("basic") else "digest"
    return {"scheme": scheme, **dict(re.findall(r'(\w+)="?([^",]*)"?', header))}


def uses_qop(challenge: dict[str, str]) -> bool:
    """Просит ли камера счётчик запросов (`qop=auth`)."""
    return "auth" in challenge.get("qop", "").split(",")


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324


def digest_header(
    user: str, password: str, challenge: dict[str, str], method: str, uri: str, nc: int
) -> str:
    """Значение заголовка `Authorization` для Digest MD5.

    Args:
        user: логин.
        password: пароль.
        challenge: результат `parse_challenge`.
        method: метод запроса.
        uri: адрес из строки запроса.
        nc: номер запроса с этим nonce; важен только при `qop=auth`.

    Returns:
        Строка `Digest …` без имени заголовка.
    """
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    header = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    if uses_qop(challenge):
        count, cnonce = f"{nc:08x}", os.urandom(8).hex()
        digest = f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}"
        header += f', qop=auth, nc={count}, cnonce="{cnonce}"'
    else:
        digest = f"{ha1}:{nonce}:{ha2}"
    if "opaque" in challenge:
        header += f', opaque="{challenge["opaque"]}"'
    return f'{header}, response="{_md5(digest)}"'
