"""RUV-E-SUBSCRIBE-1: базовые уведомления ONVIF — подписка, продление и отмена.

Заменяет тесты onvif-tt EVENT-2-1-9, -12, -28 (excluded.py): те подписывают камеру на
127.0.0.1, а камера вправе такой адрес отвергнуть. Здесь адрес получателя — из сети для
документации: камера принимает его как обычный, а уведомления никуда не доходят и стенду не
мешают. Приёма уведомлений в проверке нет: только жизненный цикл подписки.

Настройки камеры не меняются, поэтому галочка «изменяющие» не нужна: подписка живёт минуты и
снимается в конце проверки при любом исходе. Запросы несут заголовки WS-Addressing: без них
камеры на gSOAP (Рувер) отвечают отказом. Если ссылка подписки содержит ReferenceParameters,
они повторяются в заголовке каждого запроса к подписке, как требует WS-Addressing.
"""

import contextlib
import datetime
import uuid
from xml.etree.ElementTree import Element, tostring
from xml.sax.saxutils import escape

from qa_tool.checks.onvif.replay import soap_evidence
from qa_tool.checks.onvif.storage import parse_time
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    Phase,
    Severity,
    Status,
)
from qa_tool.drivers.camera.onvif_generic import service_urls, stand_service_url
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_ID = "RUV-E-SUBSCRIBE-1"
# RFC 5737, TEST-NET-1: адрес не принадлежит никому, поэтому уведомления камеры никуда не уйдут.
CONSUMER = "http://192.0.2.1:1/qa-tool"
_ACTIONS = "http://docs.oasis-open.org/wsn/bw-2"
SUBSCRIBE_ACTION = f"{_ACTIONS}/NotificationProducer/SubscribeRequest"
RENEW_ACTION = f"{_ACTIONS}/SubscriptionManager/RenewRequest"
UNSUBSCRIBE_ACTION = f"{_ACTIONS}/SubscriptionManager/UnsubscribeRequest"
RENEW_BODY = "<wsnt:Renew><wsnt:TerminationTime>PT120S</wsnt:TerminationTime></wsnt:Renew>"
UNSUBSCRIBE_BODY = "<wsnt:Unsubscribe/>"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _child(root: Element, name: str) -> Element | None:
    return next((e for e in root.iter() if _local(e.tag) == name), None)


def termination(answer: Element) -> datetime.datetime | None:
    """Срок подписки из ответа Subscribe или Renew; None — камера его не сообщила."""
    element = _child(answer, "TerminationTime")
    return parse_time(element.text or "") if element is not None else None


def subscription_gone(fault: OnvifFault) -> bool:
    """Доказывает ли отказ, что подписки больше нет.

    Смотреть можно только на элемент `ResourceUnknownFault` в Detail. Код и статус ничего не
    значат: на настоящих камерах 06.11 верный ответ приходил и как `Receiver` с HTTP 500
    (Hikvision, Beward), и как `Sender` с HTTP 200 (Dahua). Свободный текст причины тоже не
    доказательство: в нём камера может написать что угодно (замечание QA-03).
    """
    return "ResourceUnknownFault" in fault.detail.split()


def addressing(action: str, url: str, reference: str = "") -> str:
    """Заголовки WS-Addressing запроса; `reference` — параметры из ссылки подписки."""
    return (
        f"<wsa:Action>{escape(action)}</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
        f"<wsa:To>{escape(url)}</wsa:To>{reference}"
    )


def reference_parameters(answer: Element) -> str:
    """Параметры ссылки подписки как готовые элементы заголовка; пусто — их нет."""
    parameters = _child(answer, "ReferenceParameters")
    if parameters is None:
        return ""
    return "".join(tostring(e, encoding="unicode") for e in parameters)


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Подписка на события (WS-BaseNotification)",
    description="Подписываем камеру на события, продлеваем подписку и отменяем её. Ожидаем: "
    "камера принимает подписку, продление отодвигает срок, после отмены подписки больше нет.",
    phase=Phase.NETWORK,
    order=15,
    severity=Severity.MAJOR,
    timeout_s=120.0,
    group="События",
    help_id="onvif-events",
)
def subscribe(ctx: CheckContext) -> CheckResult:
    """Пройти цикл подписки: Subscribe, Renew, Unsubscribe, Renew по отменённой подписке."""
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    expected = "подписка принята, Renew продлевает срок, после Unsubscribe подписки нет"

    def failed(reason: str) -> CheckResult:
        return CheckResult(
            Status.FAILED, reason=reason, expected=expected, evidence=soap_evidence(soap)
        )

    manager, reference = "", ""
    try:
        foreign: list[str] = []
        events = service_urls(soap, ctx.camera, foreign).get("events")
        if not events:
            reason = "камера не объявляет службу событий"
            if "events" in foreign:
                reason = "служба событий на чужом хосте отброшена"
            return CheckResult.skipped(reason)
        body = (
            "<wsnt:Subscribe><wsnt:ConsumerReference>"
            f"<wsa:Address>{CONSUMER}</wsa:Address></wsnt:ConsumerReference>"
            "<wsnt:InitialTerminationTime>PT60S</wsnt:InitialTerminationTime></wsnt:Subscribe>"
        )
        try:
            answer = soap.call(events, body, addressing(SUBSCRIBE_ACTION, events))
        except OnvifFault as e:
            return failed(f"камера не приняла подписку: {e}")
        address = _child(answer, "Address")
        if address is None or not (address.text or "").strip():
            return failed("в ответе Subscribe нет адреса подписки")
        manager = stand_service_url(ctx.camera, address.text.strip()) or ""
        if not manager:
            return failed(f"адрес подписки на чужом хосте: {address.text.strip()}")
        reference = reference_parameters(answer)
        first = termination(answer)
        if first is None:
            return failed("в ответе Subscribe нет срока подписки")
        try:
            renewed = soap.call(manager, RENEW_BODY, addressing(RENEW_ACTION, manager, reference))
        except OnvifFault as e:
            return failed(f"Renew отвергнут: {e}")
        second = termination(renewed)
        if second is None or second <= first:
            return failed(f"Renew не продлил подписку: было {first}, стало {second}")
        try:
            soap.call(manager, UNSUBSCRIBE_BODY, addressing(UNSUBSCRIBE_ACTION, manager, reference))
        except OnvifFault as e:
            return failed(f"Unsubscribe отвергнут: {e}")
        unsubscribed, manager = manager, ""
        try:
            soap.call(unsubscribed, RENEW_BODY, addressing(RENEW_ACTION, unsubscribed, reference))
        except OnvifFault as e:
            if not subscription_gone(e):
                return CheckResult(
                    Status.STAND_ERROR,
                    reason=f"ответ после отмены ничего не доказывает: {e}",
                    expected=expected,
                    evidence=soap_evidence(soap),
                )
            return CheckResult(
                Status.PASSED,
                expected=expected,
                actual=f"срок подписки {first} продлён до {second}, после отмены Renew отвергнут",
                evidence=soap_evidence(soap),
            )
        manager = unsubscribed  # подписка жива: снять её ещё раз
        return failed("подписка живёт после Unsubscribe: повторный Renew принят")
    except OnvifFault as e:
        return failed(f"ONVIF: {e}")
    except CameraUnreachable as e:
        return CheckResult(
            Status.STAND_ERROR,
            reason=f"камера не ответила: {e}",
            expected=expected,
            evidence=soap_evidence(soap),
        )
    finally:
        if manager:
            with contextlib.suppress(CameraUnreachable, OnvifFault):
                soap.call(
                    manager, UNSUBSCRIBE_BODY, addressing(UNSUBSCRIBE_ACTION, manager, reference)
                )
