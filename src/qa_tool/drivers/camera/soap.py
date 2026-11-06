"""SOAP-запросы к ONVIF с WS-Security UsernameToken (PasswordDigest).

Ответы камер — недоверенные данные: разбор только через defusedxml.
"""

import base64
import datetime
import hashlib
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from defusedxml import ElementTree

from qa_tool.core.secrets import redact
from qa_tool.core.taboo import check_onvif
from qa_tool.drivers.camera import http

# Префиксы служб ONVIF, которые проверки пишут в теле запроса (<tr2:GetProfiles/> и т. п.);
# все объявляются в конверте, поэтому тело — без своих xmlns.
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "trc": "http://www.onvif.org/ver10/recording/wsdl",
    "tse": "http://www.onvif.org/ver10/search/wsdl",
    "trp": "http://www.onvif.org/ver10/replay/wsdl",
    "tan": "http://www.onvif.org/ver20/analytics/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
}
_ENVELOPE_XMLNS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items())
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
# Камеры на gSOAP сравнивают эти адреса точно: при другом типе пароль считается открытым текстом,
# дайджест с ним не совпадает, и правильный пароль получает wsse:FailedAuthentication.
_TOKEN_PROFILE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
)
_SOAP_MESSAGE_SECURITY = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0"
)


class CameraUnreachable(ConnectionError):
    """Камера не отвечает по сети."""


class OnvifFault(RuntimeError):
    """Камера ответила SOAP Fault или не SOAP: `status` — код HTTP, `code` — подкод SOAP."""

    def __init__(self, message: str, status: int = 0, code: str = "", detail: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail  # имена и текст элементов Fault: признак бывает только в Detail


# ter:NotAuthorized — код ONVIF, wsse:FailedAuthentication — код WS-Security (камеры на gSOAP,
# RUWARE TMID-018). Смотрим только на коды: причина — свободный текст камеры.
_FAILED_AUTHENTICATION = "failedauthentication"
_CREDENTIAL_CODES = ("notauthorized", _FAILED_AUTHENTICATION)
# Запас свежести WS-Security: меньшее расхождение часов не объясняет отказ, а повтор входа
# с тем же паролем только потратил бы попытку до блокировки камеры.
CLOCK_TOLERANCE_S = 5.0
# Поправка на часы камеры, по адресу службы. Общая на процесс: процесс одной камеры делает много
# клиентов, и без неё каждая проверка заново тратила бы попытку входа.
_clock_offsets: dict[str, datetime.timedelta] = {}


def credentials_rejected(fault: OnvifFault) -> bool:
    """Отказ по входу, а не дефект операции. HTTP 401 — веб-сервер камеры до службы ONVIF."""
    code = fault.code.lower()
    return fault.status == 401 or any(mark in code for mark in _CREDENTIAL_CODES)


def _camera_utc(root: Element) -> datetime.datetime:
    """Время камеры из ответа GetSystemDateAndTime. ValueError — ответа нет или он неполон."""
    utc = next((e for e in root.iter() if e.tag.split("}")[-1] == "UTCDateTime"), None)
    if utc is None:
        raise ValueError("нет UTCDateTime")
    parts = {e.tag.split("}")[-1]: e.text for e in utc.iter()}
    names = ("Year", "Month", "Day", "Hour", "Minute", "Second")
    try:
        return datetime.datetime(*(int(parts[n]) for n in names), tzinfo=datetime.UTC)
    except (KeyError, TypeError) as e:
        raise ValueError("неполный UTCDateTime") from e


@dataclass
class Exchange:
    """Один обмен с камерой; секреты уже вырезаны."""

    url: str
    request: str
    status: int
    response: str


@dataclass
class SoapClient:
    """SOAP-клиент ONVIF с WS-Security UsernameToken (PasswordDigest)."""

    username: str
    password: str
    timeout: float = 5.0
    exchanges: list[Exchange] = field(default_factory=list)

    def _security_header(self, url: str) -> str:
        nonce = os.urandom(16)
        offset = _clock_offsets.get(urlsplit(url).netloc, datetime.timedelta())
        now = datetime.datetime.now(datetime.UTC) + offset
        created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = nonce + created.encode() + self.password.encode()
        digest = base64.b64encode(hashlib.sha1(raw).digest()).decode()
        return (
            f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{_TOKEN_PROFILE}#PasswordDigest">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_SOAP_MESSAGE_SECURITY}#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>"
        )

    def call(self, url: str, body: str, soap_header: str = "") -> Element:
        """Вызвать операцию ONVIF.

        Args:
            url: адрес сервиса ONVIF.
            body: тело `<s:Body>` с одной операцией.
            soap_header: дополнительные элементы `<s:Header>`, например WS-Addressing.

        Returns:
            Первый элемент Body ответа.

        Raises:
            TabooViolation: операция запрещена — запрос не отправлен.
            CameraUnreachable: нет ответа.
            OnvifFault: SOAP Fault или ответ не SOAP.
            ValueError: ответ с DTD или ENTITY — defusedxml его не разбирает.
        """
        check_onvif(body)  # табу: сеть, логины, пароли — запрос не уходит
        try:
            return self._post(url, self._secured(url, body, soap_header))
        except OnvifFault as fault:
            if _FAILED_AUTHENTICATION not in fault.code.lower() or not self._camera_clock_differs(
                url
            ):
                raise
        return self._post(url, self._secured(url, body, soap_header))

    def _secured(self, url: str, body: str, soap_header: str = "") -> str:
        header = f"<s:Header>{self._security_header(url)}{soap_header}</s:Header>"
        return f"<s:Envelope {_ENVELOPE_XMLNS}>{header}<s:Body>{body}</s:Body></s:Envelope>"

    def _camera_clock_differs(self, url: str) -> bool:
        """Сверить часы с камерой; True — поправка изменилась, вход стоит повторить.

        wsse:FailedAuthentication приходит и на верный пароль, если часы камеры ушли: она
        считает отметку времени просроченной. GetSystemDateAndTime пароля не требует (ONVIF
        Core), поэтому сверка не тратит попытку входа. Сверяем при каждом отказе: часы камеры
        могут сдвинуться и после первой поправки — перезагрузка сбрасывает их (TMID-018 — на
        1970 год), снимок настроек возвращает время. Повтор входа — только при таком сдвиге,
        поэтому неверный пароль стоит одной лишней попытки на сдвиг, а не на каждый вызов.
        """
        host = urlsplit(url).netloc
        difference = self.camera_clock_difference_s(url)
        if difference is None:
            return False
        applied = _clock_offsets.get(host, datetime.timedelta()).total_seconds()
        if abs(difference - applied) <= CLOCK_TOLERANCE_S:
            return False
        _clock_offsets[host] = datetime.timedelta(seconds=difference)
        return True

    def camera_clock_difference_s(self, url: str) -> float | None:
        """На сколько секунд часы камеры впереди часов стенда; None — камера не сказала.

        Запрос без пароля: попытку входа он не тратит.
        """
        body = "<s:Body><tds:GetSystemDateAndTime/></s:Body>"
        request = f"<s:Envelope {_ENVELOPE_XMLNS}>{body}</s:Envelope>"
        try:
            camera_now = _camera_utc(self._post(url, request))
        except (CameraUnreachable, OnvifFault, ValueError):
            return None
        return (camera_now - datetime.datetime.now(datetime.UTC)).total_seconds()

    def _post(self, url: str, envelope: str) -> Element:
        try:
            response = http.request(
                "POST",
                url,
                content=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=self.timeout,
            )
        except http.TransportError as e:
            raise CameraUnreachable(f"{url}: нет ответа ({type(e).__name__})") from e
        known = [self.password]
        self.exchanges.append(
            Exchange(
                url, redact(envelope, known), response.status_code, redact(response.text, known)
            )
        )
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as e:
            status = response.status_code
            raise OnvifFault(f"ответ не SOAP (HTTP {status})", status=status) from e
        fault = root.find("s:Body/s:Fault", NS)
        if fault is not None:
            reason = fault.findtext(".//s:Text", default="", namespaces=NS)
            code = fault.findtext(".//s:Subcode/s:Value", default="", namespaces=NS)
            detail = " ".join(f"{e.tag.split('}')[-1]} {e.text or ''}" for e in fault.iter())
            raise OnvifFault(f"{code} {reason}".strip(), response.status_code, code, detail)
        # SOAP 1.1 и страницы ошибок веб-сервера: разбираются как XML, но это не SOAP 1.2. Код и
        # статус нужны и здесь: иначе опечатка в пароле выглядит дефектом, и проверки камеры
        # продолжают тратить попытки входа до блокировки.
        legacy = next((e for e in root.iter() if e.tag.split("}")[-1] == "faultcode"), None)
        if legacy is not None:
            code = (legacy.text or "").strip()
            raise OnvifFault(f"{code} (SOAP 1.1)", response.status_code, code)
        body_el = root.find("s:Body", NS)
        if body_el is None or not len(body_el):
            raise OnvifFault(f"пустой ответ (HTTP {response.status_code})", response.status_code)
        return body_el[0]
