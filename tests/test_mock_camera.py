import base64
import datetime
import os
import socket

import httpx

from tests.mock_camera import PASSWORD_DIGEST_TYPE, password_digest, start_fleet


def soap(operation: str, password: str = "admin") -> bytes:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
    wsu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f'<s:Header><wsse:Security xmlns:wsse="{wsse}" xmlns:wsu="{wsu}"><wsse:UsernameToken>'
        "<wsse:Username>admin</wsse:Username>"
        f'<wsse:Password Type="{PASSWORD_DIGEST_TYPE}">'
        f"{password_digest(nonce, created, password)}</wsse:Password>"
        f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security></s:Header>"
        f'<s:Body><{operation} xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body>'
        "</s:Envelope>"
    ).encode()


def test_eight_mock_cameras_return_their_passports():
    cameras, _ = start_fleet(8)
    try:
        for i, cam in enumerate(cameras, start=1):
            r = httpx.post(cam.xaddr, content=soap("GetDeviceInformation"))
            assert r.status_code == 200
            assert f"<tds:SerialNumber>SN{i:04d}</tds:SerialNumber>" in r.text
    finally:
        for cam in cameras:
            cam.stop()


def test_wrong_password_is_soap_fault():
    cameras, _ = start_fleet(1)
    try:
        r = httpx.post(cameras[0].xaddr, content=soap("GetDeviceInformation", password="bad"))
        assert r.status_code == 400
        assert "NotAuthorized" in r.text
    finally:
        cameras[0].stop()


def test_rtsp_options_answers_and_stop_closes_ports():
    cameras, _ = start_fleet(1)
    cam = cameras[0]
    with socket.create_connection(("127.0.0.1", cam.rtsp_port), timeout=2) as s:
        s.sendall(b"OPTIONS rtsp://127.0.0.1/ RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        assert s.recv(64).startswith(b"RTSP/1.0 200")
    port = cam.port
    cam.stop()
    try:
        httpx.post(f"http://127.0.0.1:{port}/onvif/device_service", content=b"", timeout=1)
        raise AssertionError("камера должна быть недоступна")
    except (httpx.ConnectError, httpx.ConnectTimeout):  # Windows не отказывает, а молчит
        pass
