"""Табу (решение директора 2026-11-03): программа никогда не меняет у камеры сетевые настройки,
логины и пароли. Проверка стоит в единой точке выхода к камере (drivers/camera/http.py) перед
отправкой: запрещённый запрос не уходит в сеть.

Сюда же отнесены операции, которые меняют сеть и учётные записи косвенно: сброс к заводским,
восстановление и импорт полной конфигурации, смена способа хранения паролей, сертификатов и TLS.
"""

import posixpath
import re
from urllib.parse import unquote, urlsplit


class TabooViolation(PermissionError):
    """Попытка отправить камере запрещённую команду. Это ошибка программы, а не камеры."""


# --- ONVIF --------------------------------------------------------------------------------------

FORBIDDEN_ONVIF = frozenset(
    op.lower()
    for op in (
        # сеть
        "SetNetworkInterfaces",
        "SetNetworkProtocols",
        "SetNetworkDefaultGateway",
        "SetDNS",
        "SetNTP",
        "SetHostname",
        "SetHostnameFromDHCP",
        "SetDynamicDNS",
        "SetZeroConfiguration",
        "SetIPAddressFilter",
        "AddIPAddressFilter",
        "RemoveIPAddressFilter",
        "SetDiscoveryMode",
        "SetRemoteDiscoveryMode",
        "SetDPAddresses",
        "SetDot11Configuration",
        "SetDot1XConfiguration",
        "CreateDot1XConfiguration",
        "DeleteDot1XConfiguration",
        # учётные записи и вход
        "CreateUsers",
        "DeleteUsers",
        "SetUser",
        "SetRemoteUser",
        "SetAccessPolicy",
        "SetHashingAlgorithm",
        "SetPasswordComplexityConfiguration",
        "SetPasswordHistoryConfiguration",
        "SetAuthFailureWarningConfiguration",
        # сертификаты и TLS (Device и Advanced Security)
        "CreateCertificate",
        "LoadCertificates",
        "LoadCACertificates",
        "DeleteCertificates",
        "SetCertificatesStatus",
        "SetClientCertificateMode",
        "LoadCertificateWithPrivateKey",
        "SetEnabledTLSVersions",
        "AddServerCertificateAssignment",
        "RemoveServerCertificateAssignment",
        "ReplaceServerCertificateAssignment",
        "CreateRSAKeyPair",
        "UploadKeyPairInPKCS8",
        "UploadCertificateWithPrivateKeyInPKCS12",
        "DeleteKey",
        "DeleteCertificate",
        "UploadCertificate",
        "UploadPassphrase",
        # сброс, восстановление, прошивка в обход сценария
        "SetSystemFactoryDefault",
        "RestoreSystem",
        "StartSystemRestore",
    )
)

_XML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_OPENING_TAG = re.compile(r"<\s*(?:[\w.-]+:)?([A-Za-z_][\w.-]*)")


def soap_operations(body: str) -> list[str]:
    """Имена всех элементов запроса (без комментариев): операцию нельзя спрятать за другой."""
    return _OPENING_TAG.findall(_XML_COMMENT.sub("", body))


def check_onvif(body: str) -> None:
    """Не пропустить SOAP-запрос, если в нём есть запрещённая операция ONVIF.

    Raises:
        TabooViolation: в запросе операция из FORBIDDEN_ONVIF.
    """
    for operation in soap_operations(body):
        if operation.lower() in FORBIDDEN_ONVIF:
            raise TabooViolation(
                f"ONVIF {operation} запрещена: программа не меняет сетевые настройки, "
                "логины и пароли камеры"
            )


# --- HTTP API адаптеров (первый — «Рувер», OpenAPI) ----------------------------------------------

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FORBIDDEN_HTTP_PREFIXES = (
    "/api/network",  # IP, маска, шлюз, DNS, DHCP, порты, RTSP-авторизация
    "/api/users",  # логины, пароли, роли, права
    "/api/snmp",  # SNMP-учётные записи
    "/api/lte",  # APN и учётные данные мобильной сети
    "/api/service/reset",  # сброс к заводским — меняет сеть и пароли
    "/api/service/import_settings",  # импорт полной конфигурации — то же
    "/api/datetime",  # NTP и источник времени: в ONVIF SetNTP закрыт так же
)
# Разделы без своей папки: настройки идут рядом с разрешёнными, поэтому только по началу имени
FORBIDDEN_HTTP_NAME_PREFIXES = ("/api/video/multicast_",)  # адреса и порты мультикаста — это сеть


def normalize_path(url_or_path: str) -> str:
    """Путь без хоста, %XX, `//`, `./`, `../` и регистра — обходы префиксов не проходят."""
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
    for _ in range(3):  # двойное кодирование %256E → %6E → n
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.split(";", 1)[0]  # параметры пути («;jsessionid=…») к разделу не относятся
    normalized = posixpath.normpath("/" + path.replace("\\", "/")).lower()
    return "/" + normalized.lstrip("/")


def check_http(method: str, url_or_path: str) -> None:
    """Не пропустить изменение запрещённых разделов API адаптера.

    Raises:
        TabooViolation: изменяющий метод по пути из FORBIDDEN_HTTP_PREFIXES.
    """
    if method.upper() not in _WRITE_METHODS:
        return
    normalized = normalize_path(url_or_path)
    in_section = any(
        normalized.startswith(prefix)
        and (len(normalized) == len(prefix) or normalized[len(prefix)] in "/.")  # «/api/users.json»
        for prefix in FORBIDDEN_HTTP_PREFIXES
    )
    if in_section or normalized.startswith(FORBIDDEN_HTTP_NAME_PREFIXES):
        raise TabooViolation(
            f"{method.upper()} {url_or_path} запрещён: программа не меняет сетевые настройки, "
            "логины и пароли камеры"
        )


# --- Веб-обход любой камеры (AD-4, AD-7) ----------------------------------------------------------
# Чужой интерфейс заранее не известен. Изменяющий запрос (в том числе GET с action=set… — так
# устроены Dahua CGI и Axis VAPIX) не пропускается, если в его пути, параметрах, теле или заголовке
# страницы есть слово-признак сети, учётных записей, сертификатов или сброса. Сравнение — по
# словам (с разбором camelCase), а не по подстрокам: «report» не ловится словом «port».
# Список широкий намеренно: лучше не нажать лишнего.

WEB_EXACT_WORDS = frozenset(
    [
        "ip",
        "ipv4",
        "ipv6",
        "mtu",
        "mac",
        "wan",
        "lan",
        "nat",
        "dns",
        "ddns",
        "ntp",
        "sip",
        "p2p",
        "lte",
        "apn",
        "upnp",
        "port",
        "ports",
        "pwd",
        "psw",
        "pass",
        "tls",
        "ssl",
        "dot1x",
        "8021x",
    ]
)
WEB_PREFIX_WORDS = (
    # сеть
    "network",
    "netcfg",
    "netinfo",
    "tcpip",
    "ipaddr",
    "dhcp",
    "gateway",
    "netmask",
    "subnet",
    "pppoe",
    "wifi",
    "wlan",
    "wireless",
    "hostname",
    "cloud",
    "сеть",
    "сети",
    "сетев",
    "шлюз",
    "маск",
    "порт",
    # учётные записи, вход, безопасность
    "user",
    "account",
    "passw",
    "pwd",
    "login",
    "logon",
    "credential",
    "auth",
    "security",
    "certificate",
    "cert",
    "snmp",
    "пользовател",
    "учётн",
    "учетн",
    "парол",
    "логин",
    "безопасн",
    "сертифик",
    # сброс, восстановление, импорт, прошивка
    # «update» и «default» не берём: они есть почти в каждой кнопке «Сохранить»
    "reset",
    "factory",
    "restore",
    "import",
    "upgrade",
    "firmware",
    "activate",
    "configurationdata",
    "сброс",
    "заводск",
    "восстанов",
    "импорт",
    "обновлен",
    "прошивк",
)
WRITE_VERBS = frozenset(
    [
        "set",
        "update",
        "add",
        "delete",
        "del",
        "remove",
        "modify",
        "apply",
        "save",
        "import",
        "reset",
        "upgrade",
        "restore",
        "activate",
        "create",
        "edit",
        "write",
        "change",
    ]
)
_SAFE_WEB_METHODS = {"GET", "HEAD", "OPTIONS"}
_CAMEL = re.compile(r"([a-zа-яё0-9])([A-ZА-ЯЁ])")
_WORD = re.compile(r"[a-zа-яё0-9]+")


def words(*texts: str) -> list[str]:
    """Слова текста в нижнем регистре; camelCase разбирается: «setNetConfig» → set, net, config."""
    joined = " ".join(t for t in texts if t)
    return _WORD.findall(_CAMEL.sub(r"\1 \2", joined).lower())


# слова, которые начинаются с признака, но к сети и учёткам не относятся
WEB_ALLOWED_WORDS = frozenset({"author", "authors", "cloudy", "portrait", "portraits"})


def web_forbidden_word(*texts: str) -> str | None:
    """Первое слово-признак сети, учётных записей, сертификатов или сброса, иначе None."""
    joined = " ".join(t for t in texts if t)
    whole = _WORD.findall(joined.lower())  # «configurationData» целиком, без разбора camelCase
    for word in words(*texts) + whole:
        if word in WEB_ALLOWED_WORDS:
            continue
        if word in WEB_EXACT_WORDS or word.startswith(WEB_PREFIX_WORDS):
            return word
    return None


def _target(url: str) -> tuple[str, str]:
    """(путь, параметры) без схемы и хоста, %XX раскрыты: «https://камера» — не признак."""
    parts = urlsplit(url)
    return unquote(parts.path), unquote(parts.query)


def is_write_request(method: str, url: str) -> bool:
    """Меняет ли запрос что-то на камере: любой метод, кроме GET/HEAD/OPTIONS,
    или GET с глаголом set/update/… в имени скрипта или параметрах (Dahua CGI, Axis VAPIX).
    """
    if method.upper() not in _SAFE_WEB_METHODS:
        return True
    path, query = _target(url)
    return any(w in WRITE_VERBS for w in words(path, query))  # глагол в любом месте пути


# Решение директора 2026-11-04: без этих трёх адресов программа не войдёт в API «Рувер», а в них
# есть слова «auth» и «login». Больше исключений из слов-признаков нет.
CAMERA_LOGIN_REQUESTS = frozenset(
    {
        ("GET", "/api/auth/create_session"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
    }
)


def is_camera_login(method: str, url: str) -> bool:
    """Запрос ли это входа в API «Рувер»: адрес совпадает буквально, без параметров.

    Адрес намеренно не приводится к общему виду: `normalize_path` убирает `;…`, `..` и %XX,
    и «/api/auth/login;/../users/set_password» прошёл бы как вход.
    """
    parts = urlsplit(url)
    plain = not (parts.query or parts.fragment or url.endswith(("?", "#")))
    return plain and (method.upper(), parts.path) in CAMERA_LOGIN_REQUESTS


def check_web_request(method: str, url: str, body: str = "", page_title: str = "") -> None:
    """Не пропустить изменяющий запрос к сети, учётным записям, сертификатам или сбросу.

    Применяется к каждому не-SOAP запросу через drivers/camera/http.py и к запросам
    браузера при веб-обходе (этап 5). SOAP проверяется по операциям (`check_onvif`).

    Raises:
        TabooViolation: запрос меняет что-то на камере и содержит слово-признак.
    """
    if not is_write_request(method, url) or is_camera_login(method, url):
        return
    path, query = _target(url)
    word = web_forbidden_word(path, query, body, page_title)
    if word:
        raise TabooViolation(
            f"{method.upper()} {url} не отправлен: похоже на изменение сети, учётных записей "
            f"или сброс (признак «{word}»)"
        )


# --- RTSP ---------------------------------------------------------------------------------------

# Методы, которые только читают поток. SET_PARAMETER, ANNOUNCE и RECORD (RFC 2326 §10) меняют
# состояние камеры, а у некоторых прошивок SET_PARAMETER открывает доступ к настройкам.
RTSP_READ_METHODS = frozenset(
    {"OPTIONS", "DESCRIBE", "SETUP", "PLAY", "PAUSE", "GET_PARAMETER", "TEARDOWN"}
)


def check_rtsp(method: str) -> None:
    """Не пропустить RTSP-запрос, который не только читает поток.

    Raises:
        TabooViolation: метод не из `RTSP_READ_METHODS`.
    """
    if method.upper() not in RTSP_READ_METHODS:
        raise TabooViolation(f"RTSP {method} не отправляется: стенд только читает поток")
