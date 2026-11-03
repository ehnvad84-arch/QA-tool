"""RUV-PROFILES-1: матрица профилей ONVIF S, G, M, T — объявлен ли профиль, работают ли его
обязательные функции.

Обязательные функции выписаны из «ONVIF Profiles Conformance Device Test Specification»
(github.com/onvif/testspecs, коммит c7b7b65b, раздел «Profile X Support Check»: пункты вида
«если X не поддерживается — профиль не поддерживается»). Текст требований — обычная речь,
надёжно разобрать его программой нельзя, поэтому список составлен вручную; тест
`tests/checks/test_profiles.py` сверяет каждую фразу `spec` с файлом спецификации.

Итог функции — по строкам протокола этой же камеры: тесты onvif-tt и наши RUV-*.
Проверка идёт последней в сетевой фазе (order 990): к этому времени они уже выполнены.
Функции, которые меняют сеть и учётные записи, проверяются только чтением (табу, AD-7).
"""

import fnmatch
import json
from dataclasses import asdict, dataclass

from qa_tool.core.checks import (
    PREVIOUS_RESULTS,
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Severity,
    Status,
)
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_ID = "RUV-PROFILES-1"
PREVIOUS = PREVIOUS_RESULTS
SCOPES = {"S": "Streaming", "G": "G", "M": "M", "T": "T"}
WORKS, BROKEN, UNSUPPORTED, UNCHECKED = "работает", "не работает", "не поддержана", "не проверена"
READ_ONLY = "проверено только чтением: изменение — табу (AD-7)"


@dataclass(frozen=True)
class Feature:
    """Обязательная функция профиля и тесты, которые её подтверждают."""

    name: str
    spec: str  # фраза из Support Check — по ней тест сверяет список со спецификацией
    tests: tuple[str, ...]  # ID и маски строк протокола (onvif-tt и RUV-*)
    note: str = ""


_DISCOVERY = Feature(
    "Обнаружение WS-Discovery",
    "Discovery feature",
    (),
    "проверяется поиском камер QA-tool, не этим прогоном",
)
_NETWORK = Feature(
    "Сетевые настройки", "Network Configuration", ("LOCAL-NETWORK-GET-*",), READ_ONLY
)
_USERS = Feature("Учётные записи", "User Handling", ("LOCAL-AUTH-*", "SECURITY-1-1-1"), READ_ONLY)
_DIGEST = Feature("Аутентификация HTTP Digest", "Digest", ("LOCAL-AUTH-*",))
_PULLPOINT = Feature(
    "События: Pull-Point",
    "Pull-Point Notification",
    ("EVENT-3-1-*", "LOCAL-EVENT-PULLPOINT-CREATE"),
)
_EVENTS = Feature("Служба событий", "Event Service", ("EVENT-*", "DEVICE-1-1-16"))
_METADATA = Feature("Метаданные", "Metadata", ("RUV-M-METADATA-1", "RUV-M-STREAM-1"))

PROFILES: dict[str, tuple[Feature, ...]] = {
    "S": (
        Feature("Возможности (GetCapabilities)", "Capabilities", ("DEVICE-1-1-2", "DEVICE-1-1-3")),
        _DISCOVERY,
        _NETWORK,
        _USERS,
        Feature("События: WS-BaseNotification", "WS-BasicNotification", ("EVENT-2-1-*",)),
        _PULLPOINT,
        Feature(
            "Аутентификация WS-UsernameToken",
            "WS-UsernameToken",
            ("SECURITY-1-1-1", "LOCAL-AUTH-VALID-CREDENTIALS"),
        ),
        Feature(
            "Медиапрофили",
            "Media Profile Configuration",
            ("LOCAL-MEDIA-S-VIDEO-SOURCES", "LOCAL-MEDIA-S-SOURCE-CONFIGS", "MEDIA2-1-1-1"),
        ),
        Feature(
            "Настройки видеокодера",
            "Video Encoder Configuration",
            ("LOCAL-MEDIA-S-ENCODER-*", "LOCAL-MEDIA-S-OPTIONS-COVER-PROFILE"),
        ),
        Feature("Настройки метаданных", "Metadata Configuration", ()),
        Feature(
            "Видеопоток RTSP",
            "Media Streaming",
            ("LOCAL-MEDIA-S-STREAM-URI", "LOCAL-MEDIA-S-RTSP-LIVE", "LOCAL-RTSS-*"),
        ),
    ),
    "G": (
        Feature("Список служб (GetServices)", "GetServices", ("DEVICE-1-1-13",)),
        _DISCOVERY,
        _NETWORK,
        _USERS,
        _EVENTS,
        _PULLPOINT,
        _DIGEST,
        Feature("Поиск записей", "Recording Search Service", ("SEARCH-*", "LOCAL-SEARCH-*")),
        Feature(
            "Воспроизведение архива",
            "Replay Service",
            ("DEVICE-1-1-20", "LOCAL-REPLAY-STREAM-URI", "RUV-G-REPLAY-1"),
        ),
        Feature(
            "Управление записью", "Recording Control Service", ("RECORDING-*", "LOCAL-RECORDING-*")
        ),
        Feature("Служба медиа", "Media Service", ("LOCAL-MEDIA-S-*", "MEDIA2-*")),
    ),
    "M": (
        Feature("Возможности (GetCapabilities)", "Capabilities", ("DEVICE-1-1-2",)),
        _DIGEST,
        Feature("Служба Media2", "Media2 Service", ("MEDIA2-1-1-4", "MEDIA2-*")),
        _METADATA,
        Feature("Поток метаданных RTSP", "Metadata RTSP streaming", ("RUV-M-STREAM-1",)),
        Feature("Видеопоток RTSP", "Video RTSP streaming", ("LOCAL-MEDIA2-RTSP-LIVE",)),
        Feature("Служба аналитики", "Analytics Service", ("ANALYTICS-3-1-*",)),
        Feature("Модули аналитики", "Analytics Module", ("ANALYTICS-4-1-*", "RUV-M-ANALYTICS-1")),
        _EVENTS,
    ),
    "T": (
        _DIGEST,
        _DISCOVERY,
        _NETWORK,
        _USERS,
        _EVENTS,
        _PULLPOINT,
        Feature("Служба Media2", "Media2 Service", ("MEDIA2-1-1-4", "MEDIA2-2-2-*")),
        Feature("Настройки видеокодера", "Video Encoder Configuration", ("MEDIA2-2-3-*",)),
        Feature(
            "Видеопоток RTSP",
            "RTSP streaming",
            ("LOCAL-MEDIA2-STREAM-URI", "LOCAL-MEDIA2-RTSP-LIVE"),
        ),
        Feature("Снимок JPEG", "JPEG Snapshot", ()),
        _METADATA,
        Feature("Надписи на кадре (OSD)", "On-Screen Display", ()),
        Feature("Служба изображения", "Imaging Service", ("IMAGING-*",)),
        Feature("Служба входов и выходов", "Device IO Service", ("DEVICEIO-*",)),
    ),
}


@dataclass
class FeatureVerdict:
    """Итог функции профиля для протокола."""

    profile: str
    feature: str
    verdict: str
    basis: str
    note: str = ""


def judge(feature: Feature, previous: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Итог функции по строкам протокола: (итог, основание — какие строки и с чем)."""
    rows = {
        test_id: status_reason
        for test_id, status_reason in previous.items()
        if any(fnmatch.fnmatch(test_id, mask) for mask in feature.tests)
    }
    if not rows:
        return UNCHECKED, "нет проверок этой функции в прогоне"
    failed = [i for i, (status, _) in rows.items() if status == Status.FAILED.value]
    passed = [i for i, (status, _) in rows.items() if status == Status.PASSED.value]
    if failed:
        return BROKEN, "не прошли: " + ", ".join(sorted(failed))
    if passed:
        return WORKS, "прошли: " + ", ".join(sorted(passed))
    not_advertised = [i for i, (_, reason) in rows.items() if "does not advertise" in reason]
    if not_advertised:
        return UNSUPPORTED, "камера не объявляет службу: " + ", ".join(sorted(not_advertised))
    return UNCHECKED, "проверки пропущены: " + ", ".join(sorted(rows))


def declared_profiles(ctx: CheckContext) -> set[str]:
    """Профили, объявленные камерой в Scopes (onvif://www.onvif.org/Profile/…)."""
    url = f"http://{ctx.camera.address}:{ctx.camera.port}/onvif/device_service"
    answer = SoapClient(ctx.camera.username, ctx.password).call(url, "<tds:GetScopes/>")
    items = {(e.text or "").strip() for e in answer.iter() if e.tag.endswith("ScopeItem")}
    return {p for p, scope in SCOPES.items() if f"onvif://www.onvif.org/Profile/{scope}" in items}


def matrix(previous: dict[str, tuple[str, str]]) -> list[FeatureVerdict]:
    """Матрица «профиль × обязательная функция» по строкам протокола."""
    out = []
    for profile, features in PROFILES.items():
        for feature in features:
            verdict, basis = judge(feature, previous)
            out.append(FeatureVerdict(profile, feature.name, verdict, basis, feature.note))
    return out


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Профили ONVIF S, G, M, T",
    description="Сверяем объявленные камерой профили с обязательными функциями по ONVIF "
    "Profiles Conformance: каждая функция подтверждается тестами onvif-tt и RUV-*.",
    phase=Phase.NETWORK,
    order=990,
    severity=Severity.MAJOR,
)
def profiles(ctx: CheckContext) -> CheckResult:
    """Строка на каждый профиль: объявлен ли он и работают ли его обязательные функции."""
    try:
        declared = declared_profiles(ctx)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult.stand_error(f"не прочитать объявленные профили (GetScopes): {e}")
    table = matrix(ctx.extra.get(PREVIOUS, {}))
    evidence = Evidence(
        "matrix.json",
        json.dumps(
            {"declared": sorted(declared), "matrix": [asdict(v) for v in table]},
            ensure_ascii=False,
            indent=1,
        ),
    )
    children = []
    for profile in PROFILES:
        rows = [v for v in table if v.profile == profile]
        bad = [
            f"{v.feature} — {v.verdict} ({v.basis})"
            for v in rows
            if v.verdict in (BROKEN, UNSUPPORTED)
        ]
        unchecked = sum(v.verdict == UNCHECKED for v in rows)
        if profile not in declared:
            result = CheckResult.skipped(f"профиль {profile} камерой не объявлен")
        elif bad:
            result = CheckResult(
                Status.FAILED,
                reason=f"профиль {profile} объявлен, но обязательные функции: " + "; ".join(bad),
                expected="все обязательные функции профиля работают",
                actual="; ".join(bad),
                evidence=[evidence],
            )
        else:
            note = f"; не проверено функций: {unchecked}" if unchecked else ""
            result = CheckResult(
                Status.PASSED,
                expected="все обязательные функции профиля работают",
                actual=f"обязательные функции профиля {profile} работают{note}",
                evidence=[evidence],
            )
        children.append(ChildResult(f"RUV-PROFILE-{profile}", f"Профиль ONVIF {profile}", result))
    return CheckResult(
        Status.PASSED,
        evidence=[evidence],
        children=children,
        data={"profiles_matrix": [asdict(v) for v in table], "declared": sorted(declared)},
    )
