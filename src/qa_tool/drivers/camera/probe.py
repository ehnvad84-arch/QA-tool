"""Проверки доступности камеры, которые ничего не меняют: открыт ли TCP-порт, отвечает ли RTSP."""

import socket


def tcp_open(address: str, port: int, timeout: float = 1.0) -> bool:
    """Принимает ли камера TCP-соединение на порт (ничего не отправляет)."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsp_answers(address: str, port: int, timeout: float) -> bool:
    """OPTIONS без авторизации: ответ «RTSP/1.0 …» (200 или 401) — сервер жив."""
    try:
        with socket.create_connection((address, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"OPTIONS rtsp://{address}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            return s.recv(64).startswith(b"RTSP/1.0")
    except OSError:
        return False
