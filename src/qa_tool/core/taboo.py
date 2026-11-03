"""Табу (решение директора 2026-11-03): программа никогда не меняет у камеры сетевые настройки,
логины и пароли. Проверка стоит в драйверах перед отправкой: запрещённый запрос не уходит в сеть.

Сюда же отнесены операции, которые меняют сеть и учётные записи косвенно: сброс к заводским
и восстановление/импорт полной конфигурации.
"""

import re


class TabooViolation(PermissionError):
    """Попытка отправить камере запрещённую команду. Это ошибка программы, а не камеры."""


# ONVIF: сеть (Device Management → Network), учётные записи, сброс и восстановление.
FORBIDDEN_ONVIF = frozenset(
    op.lower()
    for op in (
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
        "SetDot1XConfiguration",
        "CreateDot1XConfiguration",
        "DeleteDot1XConfiguration",
        "SetAccessPolicy",
        "CreateUsers",
        "DeleteUsers",
        "SetUser",
        "SetRemoteUser",
        "SetSystemFactoryDefault",
        "RestoreSystem",
        "StartSystemRestore",
    )
)

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Адаптер «Рувер» (OpenAPI): любые изменения по этим путям запрещены (чтение разрешено).
FORBIDDEN_HTTP_PREFIXES = (
    "/api/network",  # IP, маска, шлюз, DNS, DHCP, порты, RTSP-авторизация
    "/api/users",  # логины, пароли, роли, права
    "/api/snmp",  # SNMP-учётные записи
    "/api/lte",  # APN и учётные данные мобильной сети
    "/api/service/reset",  # сброс к заводским — меняет сеть и пароли
    "/api/service/import_settings",  # импорт полной конфигурации — то же
)

_OPERATION = re.compile(r"<\s*(?:[\w.-]+:)?([A-Za-z_][\w.-]*)")


def soap_operation(body: str) -> str:
    match = _OPERATION.search(body)
    return match.group(1) if match else ""


def check_onvif(body: str) -> None:
    operation = soap_operation(body)
    if operation.lower() in FORBIDDEN_ONVIF:
        raise TabooViolation(
            f"ONVIF {operation} запрещена: программа не меняет сетевые настройки, "
            "логины и пароли камеры"
        )


def check_http(method: str, path: str) -> None:
    if method.upper() not in _WRITE_METHODS:
        return
    normalized = "/" + path.split("?", 1)[0].strip("/").lower()
    for prefix in FORBIDDEN_HTTP_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise TabooViolation(
                f"{method.upper()} {path} запрещён: программа не меняет сетевые настройки, "
                "логины и пароли камеры"
            )


# Веб-обход любой камеры (AD-4, AD-7): чужой интерфейс заранее не известен, поэтому изменяющий
# запрос не пропускается, если в адресе, теле или заголовке страницы есть слово-признак.
# Список широкий намеренно: лучше не нажать лишнего.
WEB_FORBIDDEN_WORDS = (
    # сеть
    "network",
    "netcfg",
    "tcpip",
    "tcp/ip",
    "ipv4",
    "ipv6",
    "ipaddress",
    "ip_address",
    "dhcp",
    "gateway",
    "netmask",
    "subnet",
    "dns",
    "ddns",
    "pppoe",
    "upnp",
    "port",
    "8021x",
    "802.1x",
    "wifi",
    "wlan",
    "wireless",
    "ntp",
    "hostname",
    "sip",
    "p2p",
    "cloud",
    "lte",
    "apn",
    "сеть",
    "сети",
    "сетев",
    "шлюз",
    "маска",
    "порт",
    # учётные записи и безопасность
    "user",
    "account",
    "password",
    "passwd",
    "pwd",
    "login",
    "credential",
    "auth",
    "security",
    "certificate",
    "snmp",
    "пользовател",
    "учётн",
    "учетн",
    "парол",
    "логин",
    "безопасн",
    # сброс, восстановление, импорт, прошивка
    # «update» и «default» не берём: они есть почти в каждой кнопке «Сохранить»
    "reset",
    "factory",
    "restore",
    "import",
    "upgrade",
    "firmware",
    "сброс",
    "заводск",
    "восстанов",
    "импорт",
    "обновлен",
    "прошивк",
)
_SAFE_WEB_METHODS = {"GET", "HEAD", "OPTIONS"}


def web_forbidden_word(*texts: str) -> str | None:
    """Первое слово-признак сети, учётных записей или сброса в тексте, иначе None."""
    joined = " ".join(t.lower() for t in texts if t)
    return next((w for w in WEB_FORBIDDEN_WORDS if w in joined), None)


def check_web_request(method: str, url: str, body: str = "", page_title: str = "") -> None:
    """Для перехвата запросов браузером при веб-обходе любой камеры."""
    if method.upper() in _SAFE_WEB_METHODS:
        return
    word = web_forbidden_word(url, body, page_title)
    if word:
        raise TabooViolation(
            f"{method.upper()} {url} не отправлен: похоже на изменение сети, учётных записей "
            f"или сброс (признак «{word}»)"
        )
