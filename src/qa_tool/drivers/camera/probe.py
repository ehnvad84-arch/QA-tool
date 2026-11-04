"""Проверки доступности камеры, которые ничего не меняют: открыт ли TCP-порт, отвечает ли RTSP."""

import socket
from enum import Enum


def tcp_open(address: str, port: int, timeout: float = 1.0) -> bool:
    """Принимает ли камера TCP-соединение на порт (ничего не отправляет)."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


class RtspProbe(Enum):
    """Что показал OPTIONS на порту RTSP."""

    ANSWERED = "ответил"  # «RTSP/1.0 …», код 200 или 401
    CLOSED = "закрыл без ответа"  # соединение принято, ответа нет: подтверждением не считается
    NO_ANSWER = "не отвечает"  # порт закрыт, молчит или отвечает не по RTSP


def rtsp_probe(address: str, port: int, timeout: float) -> RtspProbe:
    """OPTIONS без авторизации: живой ли сервер RTSP на порту.

    Адрес потока до ONVIF неизвестен, а часть серверов (RUWARE 0.6.14) на OPTIONS не к пути потока
    закрывает соединение без ответа. Так же ведёт себя туннель без цели, поэтому такой исход —
    отдельное значение, а не «живой»: сам поток проверяет RUV-S-STREAM-1.
    """
    try:
        connection = socket.create_connection((address, port), timeout=timeout)
    except OSError:
        return RtspProbe.NO_ANSWER
    with connection as s:
        s.settimeout(timeout)
        try:
            s.sendall(f"OPTIONS rtsp://{address}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            reply = s.recv(64)
        except TimeoutError:
            return RtspProbe.NO_ANSWER
        except OSError:
            # Закрытие до чтения запроса приходит как RST, после чтения — как пустой ответ;
            # это один и тот же исход, он зависит только от момента.
            return RtspProbe.CLOSED
    if not reply:
        return RtspProbe.CLOSED
    return RtspProbe.ANSWERED if reply.startswith(b"RTSP/1.0") else RtspProbe.NO_ANSWER
