"""Табу (решение директора 2026-11-03): программа никогда не меняет у камеры сетевые настройки,
логины и пароли. Проверка стоит в единой точке выхода к камере (drivers/camera/http.py) перед
отправкой: запрещённый запрос не уходит в сеть.

Сюда же отнесены операции, которые меняют сеть и учётные записи косвенно: сброс к заводским,
восстановление и импорт полной конфигурации, смена способа хранения паролей, сертификатов и TLS.
"""

import json
import posixpath
import re
from urllib.parse import parse_qsl, unquote, urlsplit


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
# есть слова «auth» и «login». Для `http.py` других исключений из слов-признаков нет; браузер входит
# только через свои окна входа и выхода (AD-7, дополнение 2026-11-05).
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


# --- Браузер веб-обхода: опасные действия и окна (AD-7, дополнение 2026-11-05) -----------------
# Всё ниже вызывает только drivers/camera/browser.py. `http.py` эти правила не применяет, поэтому
# форматирование SD по галочке «разрушающие» работает как раньше.

WEB_DANGER_PARTS = ("reboot", "restart", "shutdown", "poweroff")  # ловит и «sysreboot»
WEB_DANGER_PREFIXES = (
    "erase",
    "purge",
    "clear",
    "delete",
    "remove",
    "перезагруз",
    "перезапуст",
    "выключ",
    "форматир",
    "удал",
    "очист",
    "стереть",
    "стира",  # не «стер»: иначе ловится «Стерео»
)
# «wipe» только целиком: /api/ptz/wiper — дворник камеры. «формат» не берём: «Формат даты».
WEB_DANGER_WORDS = frozenset({"wipe", "reset", "formatsd", "sdformat"})
# Имя параметра «format» ничего не значит: `?format=jpeg`, `"format": "H264"`. Опасно только как
# часть пути или значение.
WEB_DANGER_VALUE_WORD = "format"
WEB_DANGER_PATHS = frozenset({"/api/sdcard/prepare"})  # форматирование SD у «Рувер»
# Чтение состояния с опасным словом в адресе: камера ничего не делает, а страница без него падает.
WEB_DANGER_SAFE_READS = frozenset({"/api/service/is_reset"})

WEB_ACTION_WORDS = frozenset(
    {
        "start",
        "stop",
        "enable",
        "disable",
        "trigger",
        "move",
        "run",
        "exec",
        "cmd",
        "ptz",
        "goto",
        "toggle",
    }
)
_STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
)
# Слова входа, снятые внутри окна входа и выхода. Остальные слова-признаки действуют.
WEB_LOGIN_PREFIXES = (
    "user",
    "passw",
    "pwd",
    "pass",
    "psw",
    "login",
    "logon",
    "auth",
    "credential",
    "логин",
    "парол",
    "пользовател",
)
PASSWORD_CHANGE_WORDS = frozenset(
    {
        "new",
        "old",
        "confirm",
        "repeat",
        "change",
        "modify",
        "set",
        "reset",
        "update",
        "activate",
        "register",
        "recover",
        "forgot",
    }
)
_PASSWORD_KEY = re.compile(r"passw|pwd|psw|парол", re.IGNORECASE)
# Хеш пароля и токены случайны: регистр букв в них может сложиться в слово «Set».
_TOKEN_VALUE = re.compile(r"^[A-Za-z0-9+/=_-]{24,}$")
LOGOUT_LABELS = frozenset({"выйти", "выход из системы", "logout", "log out", "sign out", "signout"})


class WebDangerViolation(TabooViolation):
    """Опасное действие через браузер: перезагрузка, форматирование, удаление, очистка."""


def web_danger_word(*texts: str) -> str | None:
    """Первое слово опасного действия в подписи или адресе, иначе None.

    Здесь `format` опасно всегда: подписи и адреса проверяются без имён параметров.
    """
    for word in words(*texts) + _WORD.findall(" ".join(t for t in texts if t).lower()):
        if (
            word in WEB_DANGER_WORDS
            or word == WEB_DANGER_VALUE_WORD
            or word.startswith(WEB_DANGER_PREFIXES)
            or any(part in word for part in WEB_DANGER_PARTS)
        ):
            return word
    return None


def _named_parts(text: str) -> tuple[list[str], list[str]]:
    """Имена и значения из JSON или формы; прочий текст целиком считается значением."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict | list):
        names: list[str] = []
        values: list[str] = []
        todo = [data]
        while todo:
            item = todo.pop()
            if isinstance(item, dict):
                names += [str(key) for key in item]
                todo += item.values()
            elif isinstance(item, list):
                todo += item
            elif item is not None:
                values.append(str(item))
        return names, values
    pairs = parse_qsl(text, keep_blank_values=True)
    if pairs and "=" in text and not text.lstrip().startswith("<"):
        return [key for key, _ in pairs], [value for _, value in pairs]
    return [], [text] if text else []


def is_static_asset(method: str, url: str) -> bool:
    """Файл интерфейса без параметров: читается всегда, иначе `/js/reboot.js` сломал бы страницу.

    Страница `.html` с опасным словом статикой не считается: на чужой камере её открытие
    может быть действием.
    """
    parts = urlsplit(url)
    path = unquote(parts.path).lower()
    if method.upper() not in ("GET", "HEAD") or parts.query or not path.endswith(_STATIC_SUFFIXES):
        return False
    return not (path.endswith((".html", ".htm")) and web_danger_word(path))


def check_web_danger(
    method: str, url: str, body: str = "", secrets: tuple[str, ...] | None = None
) -> None:
    """Не пропустить опасное действие браузера ни при каких галочках.

    Args:
        method: метод запроса.
        url: адрес запроса.
        body: тело запроса.
        secrets: только в окнах входа и выхода: значения логина и пароля. Они и случайные токены
            не проверяются, иначе пароль `resetNew1` сорвал бы вход.

    Raises:
        WebDangerViolation: в пути, параметрах или теле есть признак опасного действия.
    """
    path, query = _target(url)
    if method.upper() in _SAFE_WEB_METHODS and path in WEB_DANGER_SAFE_READS:
        return
    if path in WEB_DANGER_PATHS:
        raise WebDangerViolation(f"{method.upper()} {url} не отправлен: опасное действие")
    query_names, query_values = _named_parts(query)
    body_names, body_values = _named_parts(body)
    if secrets is not None:
        body_values = [v for v in body_values if v not in secrets and not _TOKEN_VALUE.match(v)]
    named = [word for word in words(*query_names, *body_names) if word != WEB_DANGER_VALUE_WORD]
    word = web_danger_word(path, *query_values, *body_values, *named)
    if word:
        raise WebDangerViolation(
            f"{method.upper()} {url} не отправлен: опасное действие (признак «{word}»)"
        )


def is_web_action(method: str, url: str) -> bool:
    """Похож ли GET на действие: чужая камера может включать поворот обычным GET."""
    if method.upper() not in ("GET", "HEAD"):
        return False
    path, query = _target(url)
    return any(word in WEB_ACTION_WORDS for word in words(path, query))


def is_write_message(text: str) -> bool:
    """Меняет ли что-то сообщение WebSocket: есть глагол записи или действия."""
    return any(word in WRITE_VERBS or word in WEB_ACTION_WORDS for word in words(text))


def _window_words(url: str, body: str, secrets: tuple[str, ...]) -> list[str]:
    """Слова запроса в окне без значений логина и пароля и без случайных токенов."""
    path, query = _target(url)
    found: list[str] = []
    for text in (query, body):
        names, values = _named_parts(text)
        kept = [v for v in values if v not in secrets and not _TOKEN_VALUE.match(v)]
        found += words(*names, *kept)
    return words(path) + found


def _check_window(method: str, url: str, body: str | None, secrets: tuple[str, ...]) -> None:
    """Общие запреты окон входа и выхода."""
    if not is_write_request(method, url):
        return
    if body is None:
        raise TabooViolation(f"{method.upper()} {url} не отправлен: тело нельзя прочитать")
    for word in _window_words(url, body, secrets):
        if word in PASSWORD_CHANGE_WORDS:
            raise TabooViolation(
                f"{method.upper()} {url} не отправлен: признак смены пароля «{word}»"
            )
        if word.startswith(WEB_LOGIN_PREFIXES):
            continue
        if word in WEB_EXACT_WORDS or word.startswith(WEB_PREFIX_WORDS):
            raise TabooViolation(f"{method.upper()} {url} не отправлен: признак «{word}»")


def check_login_window(
    method: str, url: str, body: str | None, username: str, password: str
) -> None:
    """Пропустить только запрос входа внутри окна входа браузера (AD-7).

    Значения логина и пароля из проверки убираются: пароль `network1` не должен срывать вход,
    а смена пароля выдаёт себя именами полей и повтором пароля.

    Args:
        method: метод запроса.
        url: адрес запроса.
        body: тело; None — браузер не смог его прочитать.
        username: логин камеры.
        password: пароль камеры.

    Raises:
        TabooViolation: признак смены пароля, второе поле пароля или другой запрет.
    """
    _check_window(method, url, body, (username, password))
    if not is_write_request(method, url) or not body:
        return
    names, _ = _named_parts(body)
    if sum(bool(_PASSWORD_KEY.search(name)) for name in names) > 1 or (
        password and body.count(password) > 1
    ):
        raise TabooViolation(f"{method.upper()} {url} не отправлен: пароль в запросе дважды")


def check_logout_window(method: str, url: str, body: str | None) -> None:
    """Пропустить только запрос выхода внутри окна выхода браузера (AD-7).

    Raises:
        TabooViolation: признак смены пароля или другой запрет.
    """
    _check_window(method, url, body, ())


def is_logout_label(label: str) -> bool:
    """Подпись выхода из системы; «Тревожный выход» и просто «Выход» не подходят."""
    return " ".join(label.lower().split()) in LOGOUT_LABELS


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
