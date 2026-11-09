"""Родной API виртуальной камеры: Hikvision ISAPI (XML) и Dahua CGI («ключ=значение»).

Значения не хранятся отдельно, а читаются из общего состояния ONVIF (`OnvifState`): запись по ONVIF
сразу видна в родном API, как у настоящей камеры, — на этом стоит автопоиск соответствий (S4).
Параметры, которых в ONVIF нет, лежат в `extras`. Вход — Digest MD5 с `qop=auth`, `stale` и
блокировкой после N отказов, как у Hikvision.

Режимы, снятые с живых камер 09.11:
- `side_effect_like_111` — запись яркости по ONVIF сдвигает и `GainLevel` в ISAPI, и `MaxGain`
  в ONVIF;
- `sticky_like_tiandy` — ONVIF отвечает «OK», а читается прежнее значение;
- `bitrate_edge_like_108` — объявлено 512…5632 кбит/с и в ONVIF, и в CGI, а H.264 держит 1792;
- `pinned` — родной API показывает своё значение независимо от ONVIF (ExposureType manual при
  ONVIF AUTO на .111);
- `overrides` — любой сырой ответ по адресу: бомбы, мусорные диапазоны, ошибки.

Один обработчик `handle` служит и `httpx.MockTransport` (`transport()`), и HTTP-серверу
`MockCamera` (параметр `native`). Журнал `requests` — что реально дошло до камеры: на нём стоят
тесты табу.
"""

import hashlib
import re
import secrets
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from tests.mock_onvif import (
    _TT,
    IMAGING_FULL,
    MockStream,
    OnvifServices,
    OnvifState,
    encoder_now,
)

HIK_NS = "http://www.hikvision.com/ver20/XMLSchema"
REALM = "IP Camera"


@dataclass(frozen=True)
class Leaf:
    """Родной параметр виртуальной камеры.

    `onvif` — путь листа в ImagingSettings20 («Brightness», «WhiteBalance/Mode») или ключ
    кодировщика `encoder:kbps` / `encoder:encoding`; пусто — параметра в ONVIF нет, значение
    в `NativeApi.extras`. `values` — перевод перечисления ONVIF → родное; `low`, `high` —
    объявленный диапазон числа.
    """

    path: str
    onvif: str = ""
    default: str = ""
    values: tuple[tuple[str, str], ...] = ()
    low: int | None = None
    high: int | None = None
    options: tuple[str, ...] = ()


def _num(path, onvif, default="50", low=0, high=100) -> Leaf:
    return Leaf(path, onvif, default, low=low, high=high)


def _enum(path, onvif, values, default) -> Leaf:
    pairs = tuple(values.items())
    return Leaf(path, onvif, default, values=pairs, options=tuple(v for _, v in pairs))


HIK_IMAGE = (
    _num("Color/brightnessLevel", "Brightness"),
    _num("Color/contrastLevel", "Contrast"),
    _num("Color/saturationLevel", "ColorSaturation"),
    _num("Sharpness/SharpnessLevel", "Sharpness"),
    _enum(
        "WhiteBalance/WhiteBalanceStyle",
        "WhiteBalance/Mode",
        {"AUTO": "auto", "MANUAL": "manual"},
        "auto",
    ),
    _num("WhiteBalance/WhiteBalanceRed", "WhiteBalance/CrGain"),
    _num("WhiteBalance/WhiteBalanceBlue", "WhiteBalance/CbGain"),
    _enum("WDR/mode", "WideDynamicRange/Mode", {"ON": "open", "OFF": "close"}, "close"),
    _num("WDR/WDRLevel", "WideDynamicRange/Level"),
    _enum("BLC/enabled", "BacklightCompensation/Mode", {"ON": "true", "OFF": "false"}, "false"),
    _enum("Exposure/ExposureType", "Exposure/Mode", {"AUTO": "auto", "MANUAL": "manual"}, "auto"),
    _num("Gain/GainLevel", "Exposure/MaxGain", "100"),
    _enum(
        "IrcutFilter/IrcutFilterType",
        "IrCutFilter",
        {"AUTO": "auto", "ON": "day", "OFF": "night"},
        "auto",
    ),
    _enum("ImageFlip/enabled", "", {}, "false"),
    _enum("PowerLineFrequency/powerLineFrequencyMode", "", {}, "50hz"),
)
HIK_STREAM = (
    _enum("Video/videoCodecType", "encoder:encoding", {"H264": "H.264", "H265": "H.265"}, "H.264"),
    _num("Video/constantBitRate", "encoder:kbps", "4000", 256, 8192),
    _num("Video/maxFrameRate", "", "2500", 100, 2500),
    _num("Video/GovLength", "", "50", 1, 400),
)
DAHUA_COPIES = (
    3  # профили «день», «ночь», «обычный»: у Dahua каждая настройка цвета — тремя копиями
)
DAHUA_COLOR = (
    _num("Brightness", "Brightness"),
    _num("Contrast", "Contrast"),
    _num("Saturation", "ColorSaturation"),
    _num("Hue", "", "50"),
    _num("Gamma", "", "50"),
)
DAHUA_IN_OPTIONS = (
    _enum("WhiteBalance", "WhiteBalance/Mode", {"AUTO": "Auto", "MANUAL": "Custom"}, "Auto"),
    _enum("Backlight", "BacklightCompensation/Mode", {"OFF": "0", "ON": "1"}, "0"),
    _enum("ExposureMode", "Exposure/Mode", {"AUTO": "0", "MANUAL": "4"}, "0"),
    _num("Gain", "Exposure/MaxGain", "50"),
    _num("Sharpness", "Sharpness"),
)
DAHUA_ENCODE = (
    _enum("Video.Compression", "encoder:encoding", {"H264": "H.264", "H265": "H.265"}, "H.264"),
    _num("Video.BitRate", "encoder:kbps", "4096", 512, 5632),
    _enum("Video.BitRateControl", "", {}, "CBR"),
    _num("Video.FPS", "", "25", 1, 25),
    _num("Video.GOP", "", "50", 1, 150),
)


@dataclass
class NativeApi:
    """Родной API одной виртуальной камеры поверх её состояния ONVIF.

    Не потокобезопасна по записи: HTTP-сервер `MockCamera` многопоточный, но тесты шлют запросы
    к одной камере по очереди.
    """

    brand: str
    state: OnvifState = field(default_factory=OnvifState)
    username: str = "admin"
    password: str = "admin"
    model: str = "DS-2CD2043G2-I"
    firmware: str = "V5.7.3 build 220112"
    serial: str = "DS-2CD2043G2-I20220101AAWRJ00000000"
    mac: str = "00:11:22:33:44:55"
    extras: dict[str, str] = field(default_factory=dict)
    pinned: dict[str, str] = field(default_factory=dict)
    scale: dict[str, tuple[float, float]] = field(default_factory=dict)
    overrides: dict[str, tuple[int, str, bytes]] = field(default_factory=dict)
    stale_once: bool = False
    lock_after: int = 0  # 0 — не блокировать
    requests: list[str] = field(default_factory=list)
    refusals: int = 0
    locked: bool = False
    status_reads: int = 0
    stream_ids: tuple[str, ...] = ("101", "102")  # в списке потоков; читается только 101
    nonce: str = field(default_factory=lambda: secrets.token_hex(8))

    # --- вход ------------------------------------------------------------------------------

    def _challenge(self, stale: bool = False) -> dict[str, str]:
        extra = ", stale=TRUE" if stale else ""
        value = f'Digest realm="{REALM}", qop="auth", nonce="{self.nonce}"{extra}'
        return {"WWW-Authenticate": value}

    def _refuse(self, stale: bool = False) -> tuple[int, dict, bytes]:
        body = self._status(401, "Unauthorized", "notLogin" if not self.locked else "userLocked")
        return 401, self._challenge(stale) | {"Content-Type": self._type()}, body

    def _login(self, method: str, target: str, header: str) -> tuple[int, dict, bytes] | None:
        """Ответ отказа или None, если подпись Digest верна."""
        if not header.startswith("Digest "):
            return self._refuse()
        fields = _digest_fields(header[7:])
        if self.locked:
            return self._refuse()
        ha1 = _md5(f"{fields.get('username', '')}:{REALM}:{self.password}")
        ha2 = _md5(f"{method}:{fields.get('uri', '')}")
        nonce = fields.get("nonce", "")
        if fields.get("qop") == "auth":
            tail = f"{fields.get('nc', '')}:{fields.get('cnonce', '')}:auth:{ha2}"
            expected = _md5(f"{ha1}:{nonce}:{tail}")
        else:
            expected = _md5(f"{ha1}:{nonce}:{ha2}")
        signed = (
            fields.get("username") == self.username
            and fields.get("response") == expected
            # %-кодирование «[» и «]» в адресе у клиентов разное: сравнение по раскрытому
            and unquote(fields.get("uri", "")) == unquote(target)
        )
        if signed and (nonce != self.nonce or self.stale_once):
            self.stale_once = False
            self.nonce = secrets.token_hex(8)
            return self._refuse(stale=True)
        if not signed:
            self.refusals += 1
            if self.lock_after and self.refusals >= self.lock_after:
                self.locked = True
            return self._refuse()
        return None

    # --- значения поверх ONVIF ---------------------------------------------------------------

    def _imaging(self) -> ET.Element:
        return ET.fromstring(f'<root xmlns:tt="{_TT}">{self.state.imaging_xml}</root>')

    def _stream(self) -> MockStream:
        return (self.state.streams or (MockStream(self.state.profile_token),))[0]

    def _onvif_value(self, leaf: Leaf) -> str | None:
        if leaf.onvif.startswith("encoder:"):
            return str(encoder_now(self.state, self._stream())[leaf.onvif.split(":", 1)[1]])
        found = self._imaging().find(
            "/".join(f"tt:{p}" for p in leaf.onvif.split("/")), {"tt": _TT}
        )
        return None if found is None or found.text is None else found.text.strip()

    def value(self, native_id: str, leaf: Leaf) -> str:
        """Что камера покажет в родном API: закреплённое, из ONVIF по шкале или своё."""
        if native_id in self.pinned:
            return self.pinned[native_id]
        raw = self._onvif_value(leaf) if leaf.onvif else None
        if raw is None:
            return self.extras.get(native_id, leaf.default)
        if leaf.values:
            return dict(leaf.values).get(raw.upper(), raw)
        a, b = self.scale.get(native_id, (1.0, 0.0))
        try:
            return str(round(a * float(raw) + b))
        except ValueError:
            return raw

    def _range(self, leaf: Leaf) -> tuple[int, int]:
        """Объявленный диапазон: у битрейта — тот же, что объявляет ONVIF (так у .108 09.11)."""
        if leaf.onvif == "encoder:kbps":
            return self.state.encoder_kbps_range
        return leaf.low, leaf.high

    def write(self, native_id: str, leaf: Leaf, text: str) -> None:
        """Запись родным API: в ONVIF, если лист оттуда, иначе в `extras`."""
        if leaf.onvif.startswith("encoder:"):
            key = leaf.onvif.split(":", 1)[1]
            token = self._stream().token
            now = dict(self.state.encoder_now.get(token, {}))
            now[key] = {v: k for k, v in leaf.values}.get(text, text) if leaf.values else int(text)
            self.state.encoder_now[token] = now
            return
        if not leaf.onvif or self._onvif_value(leaf) is None:
            self.extras[native_id] = text
            return
        if leaf.values:
            back = {v: k for k, v in leaf.values}
            onvif = back.get(text, text)
        else:
            a, b = self.scale.get(native_id, (1.0, 0.0))
            onvif = f"{(float(text) - b) / a:g}"
        root = self._imaging()
        found = root.find("/".join(f"tt:{p}" for p in leaf.onvif.split("/")), {"tt": _TT})
        found.text = onvif
        merged = "".join(ET.tostring(c, encoding="unicode") for c in root)
        self.state.imaging_xml = merged.replace(f' xmlns:tt="{_TT}"', "")

    # --- обработчик --------------------------------------------------------------------------

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> tuple[int, dict[str, str], bytes]:
        """Ответ камеры на запрос родного API.

        Args:
            method: метод HTTP.
            target: путь с запросом, как в строке запроса (`/ISAPI/...?...`).
            headers: заголовки; нужен только `Authorization`.
            body: тело PUT.

        Returns:
            Код, заголовки и тело ответа.
        """
        self.requests.append(f"{method} {target}")
        refusal = self._login(method, target, headers.get("authorization", ""))
        if refusal is not None:
            return refusal
        if target in self.overrides:
            code, content_type, raw = self.overrides[target]
            return code, {"Content-Type": content_type}, raw
        parts = urlsplit(target)
        path, query = unquote(parts.path), dict(parse_qsl(parts.query, keep_blank_values=True))
        if self.brand == "hikvision" and path.startswith("/ISAPI/"):
            return self._isapi(method, path[len("/ISAPI/") :], body)
        if self.brand == "dahua" and path.startswith("/cgi-bin/"):
            return self._cgi(method, path[len("/cgi-bin/") :], query)
        return self._not_found()

    def transport(self) -> httpx.MockTransport:
        """`httpx.MockTransport` для подмены `http._TRANSPORT` в тестах драйвера."""

        def answer(request: httpx.Request) -> httpx.Response:
            code, headers, raw = self.handle(
                request.method, request.url.raw_path.decode(), request.headers, request.content
            )
            return httpx.Response(code, headers=headers, content=raw)

        return httpx.MockTransport(answer)

    def _type(self) -> str:
        return "application/xml" if self.brand == "hikvision" else "text/plain"

    def _not_found(self) -> tuple[int, dict, bytes]:
        if self.brand == "hikvision":
            return (
                404,
                {"Content-Type": self._type()},
                self._status(404, "Invalid Operation", "notSupport"),
            )
        return 400, {"Content-Type": self._type()}, b"Error\r\nBad Request!\r\n"

    def _status(self, code: int, text: str, sub: str) -> bytes:
        if self.brand != "hikvision":
            return f"Error\r\n{text}\r\n".encode()
        return (
            f'<?xml version="1.0" encoding="UTF-8"?><ResponseStatus version="2.0" xmlns="{HIK_NS}">'
            f"<statusCode>{4 if code >= 400 else 1}</statusCode><statusString>{text}</statusString>"
            f"<subStatusCode>{sub}</subStatusCode></ResponseStatus>"
        ).encode()

    # --- Hikvision ISAPI -------------------------------------------------------------------

    def _xml(self, root: str, inner: str) -> tuple[int, dict, bytes]:
        text = f'<?xml version="1.0" encoding="UTF-8"?><{root} version="2.0" xmlns="{HIK_NS}">{inner}</{root}>'
        return 200, {"Content-Type": self._type()}, text.encode()

    def _tree(self, prefix: str, leaves: tuple[Leaf, ...], caps: bool) -> str:
        """Вложенный XML из путей листьев; у описания — атрибуты min/max/opt, как у ISAPI."""
        groups: dict[str, list[str]] = {}
        for leaf in leaves:
            head, name = leaf.path.split("/", 1)
            attrs = ""
            if caps and leaf.low is not None:
                low, high = self._range(leaf)
                attrs = f' min="{low}" max="{high}"'
            elif caps and leaf.options:
                attrs = f' opt="{",".join(leaf.options)}"'
            value = _escape(self.value(f"{prefix}/{leaf.path}", leaf))
            groups.setdefault(head, []).append(f"<{name}{attrs}>{value}</{name}>")
        return "".join(f"<{head}>{''.join(items)}</{head}>" for head, items in groups.items())

    def _isapi(self, method: str, path: str, body: bytes) -> tuple[int, dict, bytes]:
        image = "Image/channels/1"
        if method == "PUT":
            return self._isapi_put(path, body)
        if method != "GET":
            return 405, {}, b""
        if path == "System/deviceInfo":
            return self._xml(
                "DeviceInfo",
                f"<deviceName>Camera 01</deviceName><model>{_escape(self.model)}</model>"
                f"<serialNumber>{_escape(self.serial)}</serialNumber><macAddress>{self.mac}</macAddress>"
                f"<firmwareVersion>{_escape(self.firmware)}</firmwareVersion>",
            )
        if path == "System/capabilities":
            return self._xml(
                "DeviceCap",
                "<SysCap><isSupportDst>true</isSupportDst></SysCap>"
                "<ImageCap><isSupportRegionalExposure>true</isSupportRegionalExposure></ImageCap>"
                "<isSupportStreamingEncrypt>false</isSupportStreamingEncrypt>",
            )
        if path == "System/status":
            self.status_reads += 1
            return self._xml(
                "DeviceStatus",
                f"<CPUList><CPU><cpuUtilization>{10 + self.status_reads}</cpuUtilization></CPU></CPUList>"
                f"<deviceUpTime>{1000 + self.status_reads}</deviceUpTime>",
            )
        if path in (image, image + "/capabilities"):
            caps = path.endswith("capabilities")
            return self._xml(
                "ImageChannel",
                f"<id>1</id><enabled>true</enabled>{self._tree(image, HIK_IMAGE, caps)}",
            )
        stream = "Streaming/channels/101"
        if path == "Streaming/channels":
            # список потоков: в Transport — мультикаст и способ входа, как у .114 09.11
            channel = (
                "<StreamingChannel><id>{0}</id><channelName>Camera 01</channelName>"
                "<enabled>true</enabled><Transport><Multicast><enabled>false</enabled>"
                "<destIPAddress>239.0.0.1</destIPAddress></Multicast><Security>"
                "<certificateType>digest</certificateType></Security></Transport>"
                "<Video><videoCodecType>H.264</videoCodecType></Video></StreamingChannel>"
            )
            listing = "".join(channel.format(i) for i in self.stream_ids)
            return self._xml("StreamingChannelList", listing)
        if path in (stream, stream + "/capabilities"):
            caps = path.endswith("capabilities")
            return self._xml(
                "StreamingChannel",
                f"<id>101</id><channelName>Camera 01</channelName><enabled>true</enabled>"
                f"{self._tree(stream, HIK_STREAM, caps)}",
            )
        if path == "System/Video/inputs/channels/1/overlays":
            overlays = "".join(
                f"<TextOverlay><id>{i}</id><enabled>true</enabled><positionX>{16 * i}</positionX>"
                f"<displayText>Надпись {i}</displayText></TextOverlay>"
                for i in (1, 2)
            )
            return self._xml("VideoOverlay", f"<TextOverlayList>{overlays}</TextOverlayList>")
        if path == "System/Network/interfaces/1":
            return self._xml(
                "NetworkInterface",
                "<id>1</id><IPAddress><ipVersion>v4</ipVersion><addressingType>static</addressingType>"
                "<ipAddress>127.0.0.1</ipAddress><subnetMask>255.255.255.0</subnetMask></IPAddress>"
                f"<Link><MACAddress>{self.mac}</MACAddress><MTU>1500</MTU></Link>",
            )
        if path == "Security/users":
            return self._xml(
                "UserList",
                "<User><id>1</id><userName>admin</userName><userLevel>Administrator</userLevel></User>",
            )
        return self._not_found()

    def _isapi_put(self, path: str, body: bytes) -> tuple[int, dict, bytes]:
        """Запись раздела картинки (возврат S5): известные листы из тела XML — в состояние."""
        image = "Image/channels/1/"
        if not path.startswith(image):
            return (
                403,
                {"Content-Type": self._type()},
                self._status(403, "Invalid Operation", "notSupport"),
            )
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return (
                400,
                {"Content-Type": self._type()},
                self._status(400, "Invalid XML Format", "badXmlFormat"),
            )
        group = path[len(image) :]
        for leaf in HIK_IMAGE:
            head, name = leaf.path.split("/", 1)
            if head.lower() != group.lower():
                continue
            found = next((e for e in root.iter() if e.tag.split("}")[-1] == name), None)
            if found is not None and found.text is not None:
                self.write(f"Image/channels/1/{leaf.path}", leaf, found.text.strip())
        return 200, {"Content-Type": self._type()}, self._status(200, "OK", "ok")

    # --- Dahua CGI ---------------------------------------------------------------------------

    def _dahua_tables(self) -> dict[str, list[tuple[str, Leaf]]]:
        """Ключ камеры без «table.» → лист; копии по профилям — отдельные ключи."""
        color = [
            (f"VideoColor[0][{i}].{leaf.path}", leaf)
            for i in range(DAHUA_COPIES)
            for leaf in DAHUA_COLOR
        ]
        options = [(f"VideoInOptions[0].{leaf.path}", leaf) for leaf in DAHUA_IN_OPTIONS]
        encode = [(f"Encode[0].MainFormat[0].{leaf.path}", leaf) for leaf in DAHUA_ENCODE]
        widget = [
            ("VideoWidget[0].ChannelTitle.Name", _enum("", "", {}, "Камера 01")),
            ("VideoWidget[0].ChannelTitle.EncodeBlend", _enum("", "", {}, "true")),
        ]
        return {
            "VideoColor": color,
            "VideoInOptions": options,
            "Encode": encode,
            "VideoWidget": widget,
        }

    def _text(self, lines: list[str]) -> tuple[int, dict, bytes]:
        return 200, {"Content-Type": self._type()}, ("\r\n".join(lines) + "\r\n").encode()

    def _dahua_value(self, key: str, leaf: Leaf) -> str:
        """Копии по профилям Dahua читаются из одного значения ONVIF, поэтому сдвигаются вместе.

        `pinned` можно задать и одной копии (`VideoColor[0][1].Brightness`), и всем
        (`VideoColor[0][*].Brightness`).
        """
        if key in self.pinned:
            return self.pinned[key]
        return self.value(_shared(key), leaf)

    def _cgi(self, method: str, script: str, query: dict[str, str]) -> tuple[int, dict, bytes]:
        if method != "GET":
            return 405, {}, b""
        action = query.get("action", "")
        if script == "magicBox.cgi":
            answers = {
                "getDeviceType": f"type={self.model}",
                "getSoftwareVersion": f"version={self.firmware}",
                "getSerialNo": f"sn={self.serial}",
                "getMachineName": "name=Camera 01",
                "reboot": "OK",
                "shutdown": "OK",
            }
            return self._text([answers[action]]) if action in answers else self._not_found()
        tables = self._dahua_tables()
        if script == "configManager.cgi" and action == "getConfig":
            name = query.get("name", "")
            if name == "Network":
                return self._text(
                    [
                        "table.Network.eth0.IPAddress=127.0.0.1",
                        f"table.Network.eth0.PhysicalAddress={self.mac}",
                    ]
                )
            if name not in tables:
                return self._not_found()
            return self._text(
                [f"table.{key}={self._dahua_value(key, leaf)}" for key, leaf in tables[name]]
            )
        if script == "configManager.cgi" and action == "setConfig":
            known = {key: leaf for rows in tables.values() for key, leaf in rows}
            sent = {k: v for k, v in query.items() if k != "action"}
            if not sent or any(k not in known for k in sent):
                return self._not_found()
            for key, text in sent.items():
                self.write(_shared(key), known[key], text)
            return self._text(["OK"])
        if script == "encode.cgi" and action == "getConfigCaps":
            low, high = self._range(next(x for x in DAHUA_ENCODE if x.onvif == "encoder:kbps"))
            codecs = ",".join(
                dict(DAHUA_ENCODE[0].values).get(c, c) for c in self.state.encoder_codecs
            )
            return self._text(
                [
                    # одной строкой через запятую — как у .108 09.11
                    f"caps[0].MainFormat[0].Video.BitRateOptions={low},{high}",
                    f"caps[0].MainFormat[0].Video.CompressionTypes={codecs}",
                    "caps[0].MainFormat[0].Video.FPSMax=25",
                ]
            )
        return self._not_found()


def _shared(key: str) -> str:
    """Ключ Dahua с копиями по профилям, сведёнными в одну: `VideoColor[0][2].X` → `[0][*]`."""
    return re.sub(r"^(VideoColor\[\d+\])\[\d+\]", r"\1[*]", key)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _digest_fields(text: str) -> dict[str, str]:
    """Поля заголовка Digest; значения в кавычках могут содержать запятые (uri с запросом)."""
    fields: dict[str, str] = {}
    rest = text.strip()
    while rest:
        name, _, rest = rest.partition("=")
        rest = rest.lstrip()
        if rest.startswith('"'):
            value, _, rest = rest[1:].partition('"')
        else:
            value, _, rest = rest.partition(",")
            rest = "," + rest if rest else ""
        fields[name.strip().lstrip(",").strip()] = value.strip()
        rest = rest.lstrip().removeprefix(",").lstrip()
    return fields


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- режимы живых камер 09.11 --------------------------------------------------------------


def with_max_gain(state: OnvifState, value: str = "100") -> None:
    """Добавить `Exposure/MaxGain` в настройки картинки ONVIF: у Hikvision он есть, у mock — нет."""
    xml = state.imaging_xml
    if "<tt:MaxGain>" in xml:
        return
    tag = f"<tt:MaxGain>{value}</tt:MaxGain>"
    if "<tt:Exposure>" in xml:
        state.imaging_xml = xml.replace("<tt:Exposure>", "<tt:Exposure>" + tag, 1)
    else:
        state.imaging_xml = xml + f"<tt:Exposure><tt:Mode>AUTO</tt:Mode>{tag}</tt:Exposure>"


def side_effect_like_111(
    api: NativeApi, trigger: str = "Brightness", gain_after: str = "0"
) -> None:
    """Hikvision .111: запись `trigger` по ONVIF сдвигает и MaxGain (а с ним — GainLevel в ISAPI)."""
    with_max_gain(api.state)

    def hook(state: OnvifState, names: set[str]) -> None:
        if trigger in names:
            state.imaging_xml = _set_leaf(state.imaging_xml, "Exposure/MaxGain", gain_after)

    api.state.imaging_hooks.append(hook)


def sticky_like_tiandy(state: OnvifState, field_name: str, value: str) -> None:
    """Tiandy: запись поля по ONVIF отвечает «OK», а читается прежнее значение."""
    state.imaging_sticky[field_name] = value


def bitrate_edge_like_108(state: OnvifState, cap_kbps: int = 1792) -> None:
    """Dahua .108: объявлено 512…5632 кбит/с, а при H.264 камера держит не больше `cap_kbps`."""
    state.encoder_kbps_range = (512, 5632)
    state.encoder_kbps_cap["H264"] = cap_kbps


def _set_leaf(imaging_xml: str, path: str, text: str) -> str:
    root = ET.fromstring(f'<root xmlns:tt="{_TT}">{imaging_xml}</root>')
    found = root.find("/".join(f"tt:{p}" for p in path.split("/")), {"tt": _TT})
    if found is not None:
        found.text = text
    merged = "".join(ET.tostring(c, encoding="unicode") for c in root)
    return merged.replace(f' xmlns:tt="{_TT}"', "")


def hikvision(**kwargs) -> NativeApi:
    """Виртуальный Hikvision с полным блоком картинки ONVIF (как у «Картинки»)."""
    state = kwargs.pop("state", None) or OnvifState(imaging_xml=IMAGING_FULL)
    api = NativeApi("hikvision", state, **kwargs)
    with_max_gain(api.state)
    return api


def dahua(**kwargs) -> NativeApi:
    """Виртуальная Dahua: модель и прошивка — как у .108 09.11, без серийного номера стенда."""
    state = kwargs.pop("state", None) or OnvifState(
        imaging_xml=IMAGING_FULL, encoder_kbps_range=(512, 5632)
    )
    defaults = {
        "model": "IPC-HFW2431S-S-S2",
        "firmware": "2.800.0000000.25.R",
        "serial": "7J0000000000000",
    }
    api = NativeApi("dahua", state, **(defaults | kwargs))
    with_max_gain(api.state, "50")
    return api


def onvif_set_imaging(api: NativeApi, inner: str) -> None:
    """Запись картинки по ONVIF тем же обработчиком, что у виртуальной ONVIF-камеры."""
    services = OnvifServices(api.state, "http://127.0.0.1", "rtsp://127.0.0.1/live")
    request = ET.fromstring(
        '<timg:SetImagingSettings xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><timg:VideoSourceToken>vs-1'
        f"</timg:VideoSourceToken><timg:ImagingSettings>{inner}</timg:ImagingSettings>"
        "</timg:SetImagingSettings>"
    )
    assert services.op_SetImagingSettings(request) is not None
