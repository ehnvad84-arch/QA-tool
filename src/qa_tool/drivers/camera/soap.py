"""SOAP-запросы к ONVIF с WS-Security UsernameToken (PasswordDigest).

Ответы камер — недоверенные данные: разбор только через defusedxml.
"""

import base64
import datetime
import hashlib
import os
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

from qa_tool.core.secrets import redact
from qa_tool.core.taboo import check_onvif

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"


class CameraUnreachable(ConnectionError):
    """Камера не отвечает по сети."""


class OnvifFault(RuntimeError):
    """Камера ответила SOAP Fault."""


@dataclass
class Exchange:
    """Один обмен с камерой; секреты уже вырезаны."""

    url: str
    request: str
    status: int
    response: str


@dataclass
class SoapClient:
    username: str
    password: str
    timeout: float = 5.0
    exchanges: list[Exchange] = field(default_factory=list)

    def _security_header(self) -> str:
        nonce = os.urandom(16)
        created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = nonce + created.encode() + self.password.encode()
        digest = base64.b64encode(hashlib.sha1(raw).digest()).decode()
        return (
            f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}"><wsse:UsernameToken>'
            f"<wsse:Username>{self.username}</wsse:Username>"
            f'<wsse:Password Type="{_WSSE[:-4]}-username-token-profile-1.0#PasswordDigest">'
            f"{digest}</wsse:Password>"
            f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>"
        )

    def call(self, url: str, body: str) -> Element:
        check_onvif(body)  # табу: сеть, логины, пароли — запрос не уходит
        envelope = (
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:tt="{NS["tt"]}">'
            f"<s:Header>{self._security_header()}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        try:
            response = httpx.post(
                url,
                content=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=self.timeout,
            )
        except httpx.TransportError as e:
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
            raise OnvifFault(f"ответ не SOAP (HTTP {response.status_code})") from e
        fault = root.find("s:Body/s:Fault", NS)
        if fault is not None:
            reason = fault.findtext(".//s:Text", default="", namespaces=NS)
            code = fault.findtext(".//s:Subcode/s:Value", default="", namespaces=NS)
            raise OnvifFault(f"{code} {reason}".strip())
        body_el = root.find("s:Body", NS)
        if body_el is None or not len(body_el):
            raise OnvifFault("пустой ответ")
        return body_el[0]
