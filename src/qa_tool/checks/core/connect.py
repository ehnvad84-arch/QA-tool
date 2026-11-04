"""CORE-CONNECT-1: камера доступна по сети, ONVIF отвечает, паспорт — в шапку протокола."""

from qa_tool.core.checks import (
    CREDENTIALS_REJECTED,
    REGISTRY,
    UNREACHABLE,
    CheckContext,
    CheckResult,
    Evidence,
    Severity,
    Status,
)
from qa_tool.drivers.camera.onvif_generic import OnvifCamera
from qa_tool.drivers.camera.probe import rtsp_answers, tcp_open
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

PASSPORT_KEY = "passport"
NOT_AUTHORIZED = (
    "NotAuthorized"  # подкод SOAP Fault ter:NotAuthorized — общий код отказа в доступе ONVIF
)


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
    """Проверить, что камера доступна по HTTP и RTSP и отдаёт по ONVIF свой паспорт.

    Паспорт (производитель, модель, прошивка, серийный номер) кладётся в
    `data[PASSPORT_KEY]` и уходит в шапку протокола. Закрытый HTTP-порт —
    «не прошла» с признаком `UNREACHABLE`: по нему рабочий процесс камеры пропускает
    остальные её проверки, а не копит одинаковые задачи.
    """
    cam, timeout = ctx.camera, float(ctx.extra.get("timeout_s", 5.0))
    expected = f"HTTP {cam.port} и RTSP {cam.rtsp_port} отвечают, ONVIF отдаёт паспорт"
    problems = []
    if not tcp_open(cam.address, cam.port, timeout):
        return CheckResult(
            Status.FAILED,
            reason=f"нет связи по HTTP (порт {cam.port})",
            expected=expected,
            actual="порт закрыт или камера не в сети",
            data={UNREACHABLE: True},
        )
    if not rtsp_answers(cam.address, cam.rtsp_port, timeout):
        problems.append(f"RTSP (порт {cam.rtsp_port}) не отвечает")
    driver = OnvifCamera(cam, ctx.password, timeout)
    try:
        passport = driver.get_device_information()
    except OnvifFault as e:
        if NOT_AUTHORIZED in str(e):
            # Решение директора 2026-11-03: неверный логин или пароль — ошибка настройки
            # стенда и подключения, а не дефект камеры; задачи программистам нет.
            result = CheckResult.stand_error(
                f"камера не приняла логин или пароль места {cam.slot} — проверьте их на экране "
                f"«Камеры» ({e})"
            )
            result.data[CREDENTIALS_REJECTED] = True
            return result
        problems.append(f"ONVIF GetDeviceInformation: {e}")
        passport = None
    except CameraUnreachable as e:
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
