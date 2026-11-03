"""CORE-CONNECT-1: камера доступна по сети, ONVIF отвечает, паспорт — в шапку протокола."""

import socket

from qa_tool.core.checks import REGISTRY, CheckContext, CheckResult, Evidence, Severity, Status
from qa_tool.drivers.camera.onvif_generic import OnvifCamera
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

PASSPORT_KEY = "passport"


def _tcp_open(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def _rtsp_answers(address: str, port: int, timeout: float) -> bool:
    """OPTIONS без авторизации: 200 или 401 — сервер жив."""
    try:
        with socket.create_connection((address, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"OPTIONS rtsp://{address}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            return s.recv(64).startswith(b"RTSP/1.0")
    except OSError:
        return False


@REGISTRY.check(
    "CORE-CONNECT-1",
    module="core",
    title="Связь с камерой и паспорт",
    description=(
        "Открываем HTTP- и RTSP-порт камеры, запрашиваем ONVIF GetDeviceInformation. "
        "Ожидаем: оба порта отвечают, камера отдаёт производителя, модель, прошивку "
        "и серийный номер."
    ),
    severity=Severity.BLOCKER,
    order=0,
    timeout_s=30,
)
def connect(ctx: CheckContext) -> CheckResult:
    cam, timeout = ctx.camera, float(ctx.extra.get("timeout_s", 5.0))
    expected = f"HTTP {cam.port} и RTSP {cam.rtsp_port} отвечают, ONVIF отдаёт паспорт"
    problems = []
    if not _tcp_open(cam.address, cam.port, timeout):
        return CheckResult(
            Status.FAILED,
            reason=f"нет связи по HTTP (порт {cam.port})",
            expected=expected,
            actual="порт закрыт или камера не в сети",
        )
    if not _rtsp_answers(cam.address, cam.rtsp_port, timeout):
        problems.append(f"RTSP (порт {cam.rtsp_port}) не отвечает")
    driver = OnvifCamera(cam, ctx.password, timeout)
    try:
        passport = driver.get_device_information()
    except (CameraUnreachable, OnvifFault) as e:
        problems.append(f"ONVIF GetDeviceInformation: {e}")
        passport = None
    evidence = [
        Evidence(f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}")
        for i, x in enumerate(driver.exchanges, start=1)
    ]
    data = {}
    if passport is not None:
        data[PASSPORT_KEY] = vars(passport)
        missing = [
            k for k in ("manufacturer", "model", "firmware", "serial") if not data[PASSPORT_KEY][k]
        ]
        if missing:
            problems.append("в паспорте пусто: " + ", ".join(missing))
    actual = (
        f"{passport.manufacturer} {passport.model}, прошивка {passport.firmware}, "
        f"серийный {passport.serial}"
        if passport
        else "паспорт не получен"
    )
    status = Status.FAILED if problems else Status.PASSED
    return CheckResult(
        status,
        reason="; ".join(problems),
        expected=expected,
        actual=actual,
        evidence=evidence,
        data=data,
    )
