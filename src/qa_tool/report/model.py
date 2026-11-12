"""Данные протокола из базы прогона — общие для HTML, PDF и Excel."""

import json
from dataclasses import dataclass, field

from qa_tool import __version__
from qa_tool.core.checks import Status
from qa_tool.core.models import Place
from qa_tool.core.secrets import redact
from qa_tool.core.store import RunStore
from qa_tool.report.codec import CodecAppendix, build_codec_appendix
from qa_tool.report.codec_history import attach_history
from qa_tool.report.image import ImageAppendix, build_image_appendix

MODULE_TITLES = {
    "core": "Связь",
    "onvif": "ONVIF",
    "api": "API",
    "web": "Веб-интерфейс",
    "io": "Интерфейсы",
    "lens": "Объектив и фокус",
    "daynight": "День/ночь и ИК",
    "stream": "Поток и стресс",
    "codec": "Кодеки",
    "sdcard": "SD-карта",
    "image": "Картинка",
    "analytics": "Аналитика",
}
SEVERITY_ORDER = {"блокер": 0, "серьёзная": 1, "мелкая": 2}
WEB_ROW_PREFIX = "WEB-"


@dataclass
class Row:
    """Строка «Результаты проверок» протокола."""

    number: int
    check_id: str
    module: str
    title: str
    description: str
    expected: str
    actual: str
    status: str
    reason: str
    threshold: str
    severity: str
    evidence: str


@dataclass
class Task:
    """Задача программистам по проверке, которая не прошла."""

    number: int
    title: str
    module: str
    severity: str
    steps: list[str]
    expected: str
    actual: str
    evidence: str


@dataclass
class Protocol:
    """Всё содержимое протокола камеры — общее для HTML, PDF и Excel."""

    protocol_id: str
    run_id: str
    started: str
    finished: str
    place: str
    slot: int
    address: str
    vendor: str
    passport: dict[str, str]
    scenario: dict
    version: str
    rows: list[Row]
    tasks: list[Task]
    counts: dict[str, int]
    modules: list[tuple[str, dict[str, int]]]
    verdict: str
    verdict_kind: str  # вид заключения: ok, fail, incomplete
    camera_name: str = field(default="")
    profiles: dict = field(default_factory=dict)  # матрица RUV-PROFILES-1: declared, matrix
    image: ImageAppendix | None = None  # Приложение 1 «Картинка»; None — строк IMAGE-* нет
    codec: CodecAppendix | None = None  # «Кодеки» — после «Картинки»; None — строк CODEC-* нет

    @property
    def appendices(self) -> list[tuple[int, str]]:
        """Выпущенные приложения по порядку: номер и название — для строки «Приложения»."""
        found = []
        if self.image:
            found.append((1, "Картинка: снимки и замеры"))
        if self.codec:
            found.append((self.codec.number, "Кодеки: качество на битрейт"))
        return found


def _threshold(row) -> str:
    if row["threshold_value"] is None:
        return ""
    unit = f" {row['threshold_unit']}" if row["threshold_unit"] else ""
    return f"{row['threshold_value']:g}{unit} ({row['threshold_source']})"


def _counts(statuses: list[str]) -> dict[str, int]:
    return {s.value: statuses.count(s.value) for s in Status}


def verdict(counts: dict[str, int]) -> tuple[str, str]:
    """Заключение протокола и его вид (ok, fail, incomplete) по счётчикам статусов."""
    if counts[Status.STAND_ERROR.value]:
        return "Испытания не завершены: есть ошибки стенда", "incomplete"
    if counts[Status.FAILED.value]:
        return f"Не соответствует: замечаний — {counts[Status.FAILED.value]}", "fail"
    return "Соответствует проверенным требованиям", "ok"


def _without_repeats(failed: list[Row]) -> list[tuple[Row, list[str]]]:
    """Задачи без дублей: кнопка из шапки веб-интерфейса падает одинаково на каждой странице.

    Сводятся только строки веб-обхода: у остальных модулей одинаковый текст — разные проверки.

    Returns:
        Первая строка и номера таких же строк.
    """
    groups: dict[tuple[str, str, str], tuple[Row, list[str]]] = {}
    out = []
    for row in failed:
        key = (row.title, row.reason, row.actual)
        if row.check_id.startswith(WEB_ROW_PREFIX) and key in groups:
            groups[key][1].append(row.check_id)
            continue
        entry = (row, [])
        groups[key] = entry
        out.append(entry)
    return out


def build_protocol(store: RunStore, slot: int) -> Protocol:
    """Собрать протокол камеры из базы прогона; задачи — только из «не прошла»."""
    run = store.run_info()
    cam = next(c for c in store.cameras() if c["slot"] == slot)
    passport = {k: cam[k] for k in ("manufacturer", "model", "firmware", "serial", "hardware")}
    rows, tasks = [], []
    for number, r in enumerate(store.results(slot=slot), start=1):
        row = Row(
            number=number,
            check_id=r["check_id"],
            module=MODULE_TITLES.get(r["module"], r["module"]),
            title=r["title"],
            description=r["description"],
            expected=redact(r["expected"] or ""),
            actual=redact(r["actual"] or ""),
            status=r["status"],
            reason=redact(r["reason"] or ""),
            threshold=_threshold(r),
            severity=r["severity"],
            evidence=r["evidence_dir"] or "",
        )
        rows.append(row)
    place = Place(run["place"]).title
    scenario = json.loads(run["scenario"] or "{}")
    model = (
        " ".join(v for v in (passport["manufacturer"], passport["model"]) if v) or cam["address"]
    )
    failed = [r for r in rows if r.status == Status.FAILED.value]
    failed.sort(key=lambda r: (SEVERITY_ORDER.get(r.severity, 9), r.number))
    for number, (row, same) in enumerate(_without_repeats(failed), start=1):
        firmware = passport["firmware"] or "?"
        title = f"[{row.check_id}] {row.title}" + (f": {row.reason}" if row.reason else "")
        if same:
            title += f" (то же: {', '.join(same)})"
        tasks.append(
            Task(
                number=number,
                title=title,
                module=row.module,
                severity=row.severity,
                steps=[
                    f"Камера {model}, прошивка {firmware}, адрес {cam['address']}; "
                    f"стенд — {place}.",
                    row.description,
                    f"Наблюдаемый результат: {row.actual or row.reason}.",
                ],
                expected=row.expected,
                actual=row.actual or row.reason,
                evidence=row.evidence,
            )
        )
    counts = _counts([r.status for r in rows])
    modules: dict[str, list[str]] = {}
    for row in rows:
        modules.setdefault(row.module, []).append(row.status)
    text, kind = verdict(counts)
    allowed = bool(scenario.get("allow_writes"))
    image = build_image_appendix(rows, store.run_dir, allowed)
    codec = build_codec_appendix(rows, store.run_dir, allowed, 2 if image else 1)
    if codec is not None:
        attach_history(codec, store.run_dir, passport)
    return Protocol(
        protocol_id=store.protocol_id(slot),
        run_id=store.run_id,
        started=run["started"] or "",
        finished=run["finished"] or "",
        place=place,
        slot=slot,
        address=cam["address"],
        vendor=cam["vendor"],
        passport=passport,
        scenario=scenario,
        version=__version__,
        rows=rows,
        tasks=tasks,
        counts=counts,
        modules=[(m, _counts(s)) for m, s in modules.items()],
        verdict=text,
        verdict_kind=kind,
        camera_name=model,
        profiles=_profiles_matrix(store, rows),
        image=image,
        codec=codec,
    )


def _profiles_matrix(store: RunStore, rows: list[Row]) -> dict:
    """Матрица профилей ONVIF из доказательства RUV-PROFILE-* (если проверка была в прогоне)."""
    for row in rows:
        if row.check_id.startswith("RUV-PROFILE-") and row.evidence:
            path = store.run_dir / row.evidence / "matrix.json"
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


@dataclass
class Summary:
    """Содержимое сводного отчёта: протоколы всех камер и матрица проверка × камера."""

    run_id: str
    started: str
    finished: str
    place: str
    version: str
    protocols: list[Protocol]
    checks: list[tuple[str, str]]  # (check_id, название) — строки матрицы
    matrix: dict[str, dict[int, str]]  # check_id → место → статус
    image_compare: bool = False  # выпущено «Сравнение картинки» — в сводке ссылка на него
    codec_compare: bool = False  # выпущено «Сравнение кодеков» — в сводке ссылка на него


def build_summary(store: RunStore) -> Summary:
    """Собрать сводный отчёт прогона."""
    protocols = [build_protocol(store, c["slot"]) for c in store.cameras()]
    checks: dict[str, str] = {}
    matrix: dict[str, dict[int, str]] = {}
    for p in protocols:
        for row in p.rows:
            checks.setdefault(row.check_id, row.title)
            matrix.setdefault(row.check_id, {})[p.slot] = row.status
    run = store.run_info()
    return Summary(
        run_id=store.run_id,
        started=run["started"] or "",
        finished=run["finished"] or "",
        place=Place(run["place"]).title,
        version=__version__,
        protocols=protocols,
        checks=list(checks.items()),
        matrix=matrix,
    )
