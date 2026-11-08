import socket
import struct
import threading

import pytest

from qa_tool.drivers.camera.probe import RtspProbe, rtsp_probe


@pytest.fixture
def server():
    """Сервер на случайном порту: `server(ответ)`; None — принимает и молчит."""
    listeners = []

    def start(reply: bytes | None) -> int:
        listener = socket.create_server(("127.0.0.1", 0))
        listeners.append(listener)

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(256)
                if reply is None:
                    connection.recv(256)  # молчим, пока клиент не закроет соединение
                else:
                    connection.sendall(reply)

        threading.Thread(target=serve, daemon=True).start()
        return listener.getsockname()[1]

    yield start
    for listener in listeners:
        listener.close()


def test_rtsp_reply_means_alive(server):
    assert (
        rtsp_probe("127.0.0.1", server(b"RTSP/1.0 401 Unauthorized\r\n\r\n"), 2)
        is RtspProbe.ANSWERED
    )


def test_server_closing_without_reply_is_not_an_answer(server):
    # RUWARE 0.6.14: на OPTIONS не к пути потока закрывает соединение без ответа
    assert rtsp_probe("127.0.0.1", server(b""), 2) is RtspProbe.CLOSED


def test_foreign_protocol_is_not_rtsp(server):
    reply = b"HTTP/1.1 400 Bad Request\r\n\r\n"
    assert rtsp_probe("127.0.0.1", server(reply), 2) is RtspProbe.NO_ANSWER


def test_silent_server_is_not_alive(server):
    assert rtsp_probe("127.0.0.1", server(None), 0.3) is RtspProbe.NO_ANSWER


def test_closed_port_is_not_alive():
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
    assert rtsp_probe("127.0.0.1", port, 1) is RtspProbe.NO_ANSWER


def test_reset_before_reading_request_is_same_as_closing():
    # закрытие с непрочитанным запросом приходит клиенту как RST, а не как пустой ответ
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        connection, _ = listener.accept()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        assert rtsp_probe("127.0.0.1", listener.getsockname()[1], 2) is RtspProbe.CLOSED
    finally:
        listener.close()
