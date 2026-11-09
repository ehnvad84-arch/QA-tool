"""Сверка родного API с ONVIF по таблице пар (S3, S6): строки `API-MATCH-<n>`, `API-RANGE-<n>`.

Одна и та же настройка в родном API и в ONVIF должна читаться одинаково с учётом шкалы: иначе
программисты камеры получают задачу (живой случай 09.11: Hikvision .111 — ONVIF Exposure AUTO,
ISAPI ExposureType manual). Сверяются значения снимка M0 и ONVIF state0 — до любой записи.

Шкалы: «линейная» — `родное = a·onvif + b`; «по диапазонам» — `a`, `b` из объявленных диапазонов
обеих сторон (MaxGain ↔ GainLevel); «перечисление» — таблица значений; «по точкам» — кандидат,
не сверяется. Допуски — пороги `api.compare_tol_pct` и `api.range_tol_pct` (thresholds.yaml).
Значения камеры в тексте строк — недоверенные; их экранирует шаблон протокола.
"""

from dataclasses import dataclass

from qa_tool.checks.api.catalog import BY_N, Entry
from qa_tool.core.api_map import NativeMap, Pair, Param, Sensitivity, finite_number
from qa_tool.core.checks import CheckResult, ChildResult, Evidence, Severity, Status

NO_PAIRS = "нет таблицы соответствий для модели — нужен прогон с галочкой «изменяющие»"
_NUMERIC = ("линейная", "по диапазонам")
_NUMERIC_TYPES = ("целое", "число")


@dataclass(frozen=True)
class OnvifSide:
    """Лист ONVIF камеры до записи: значение, объявленный диапазон и что камера держит на деле.

    `held` — наибольшее значение, которое камера приняла при автопоиске (битрейт: «объявлено,
    но не держит», Dahua .108 09.11); `held_note` — условие («при H.264»).
    """

    value: str | None
    low: float | None = None
    high: float | None = None
    held: float | None = None
    held_note: str = ""


@dataclass(frozen=True)
class Tolerances:
    """Допуски сверки, % от диапазона родного параметра."""

    value_pct: float = 1.0
    range_pct: float = 1.0


def number(x: float) -> str:
    """Число для протокола: без лишних нулей, с запятой."""
    return f"{x:.6g}".replace(".", ",")


def _native_range(param: Param) -> tuple[float, float, float] | None:
    """Объявленный диапазон родного параметра (мин, макс, шаг) или None."""
    allowed = param.allowed or {}
    if "мин" not in allowed or "макс" not in allowed:
        return None
    return float(allowed["мин"]), float(allowed["макс"]), float(allowed.get("шаг", 0) or 0)


def _onvif_range(side: OnvifSide) -> tuple[float, float] | None:
    """Объявленный диапазон ONVIF, если он годный."""
    if side.low is None or side.high is None or side.low > side.high:
        return None
    return side.low, side.high


def _scale(pair: Pair, side: OnvifSide, param: Param) -> tuple[float, float] | str:
    """Коэффициенты `a`, `b` шкалы или причина, почему их нет."""
    if pair.scale.kind == "линейная":
        if pair.scale.a is None or pair.scale.b is None or pair.scale.a == 0:
            return "у линейной шкалы нет коэффициентов"
        return pair.scale.a, pair.scale.b
    onvif, native = _onvif_range(side), _native_range(param)
    if onvif is None or native is None or onvif[1] == onvif[0]:
        return "шкала «по диапазонам», а годного диапазона нет с обеих сторон"
    a = (native[1] - native[0]) / (onvif[1] - onvif[0])
    return a, native[0] - a * onvif[0]


def _describe_scale(pair: Pair, a: float | None = None, b: float | None = None) -> str:
    """Шкала словами для строки протокола."""
    if pair.scale.kind == "перечисление":
        return "шкала: перечисление"
    text = f"шкала: {pair.scale.kind}"
    if a is not None:
        text += f", ×{number(a)}" + (f" {'+' if b >= 0 else '−'} {number(abs(b))}" if b else "")
    return text


@dataclass
class _Row:
    """Заготовка строки: пара, её номер в каталоге и подписи."""

    pair: Pair
    entry: Entry
    label: str

    @property
    def paths(self) -> str:
        """Оба пути для доказательства."""
        return f"ONVIF {self.entry.onvif} ↔ {self.label} {self.pair.native}"

    def child(self, prefix: str, result: CheckResult, severity: Severity | None) -> ChildResult:
        """Строка протокола с доказательством `pair.txt`."""
        result.evidence.append(
            Evidence(
                "pair.txt",
                f"{self.paths}\n{_describe_scale(self.pair)}\nисточник: {self.pair.source}\n"
                f"итог: {result.status}: {result.actual or result.reason}\n",
            )
        )
        title = f"{self.entry.title}: {'значение' if prefix == 'API-MATCH' else 'диапазон'}"
        return ChildResult(f"{prefix}-{self.entry.n}", title, result, severity=severity)


def _skip_reason(param: Param | None, pair: Pair, label: str) -> str:
    """Почему пару не сверить по карте этой камеры; пусто — можно."""
    if param is None or param.value is None or param.sensitivity is not Sensitivity.NORMAL:
        return (
            f"параметра {label} {pair.native} нет в карте камеры — пара из библиотеки не подходит"
        )
    if pair.scale.kind in _NUMERIC and param.type not in _NUMERIC_TYPES:
        return f"{label} {pair.native} — не число ({param.type}): пара из библиотеки не подходит"
    if param.volatile:
        return "значение меняется само (автоматика камеры)"
    return ""


def _match(row: _Row, param: Param, side: OnvifSide, tol: Tolerances) -> CheckResult:
    """Строка `API-MATCH-<n>`: значение пары."""
    pair, entry, label = row.pair, row.entry, row.label
    expected = f"ONVIF и {label} показывают одну настройку одинаково; источник пары: {pair.source}"
    if side.value is None:
        return CheckResult.skipped(f"ONVIF не отдал {entry.onvif}")
    if pair.scale.kind == "перечисление":
        table = {k.casefold(): v for k, v in (pair.scale.values or {}).items()}
        wanted = table.get(side.value.strip().casefold())
        if wanted is None:
            return CheckResult.skipped(
                f"значение {side.value} не встречалось при поиске и нет в ручной таблице"
            )
        actual = f"ONVIF {entry.onvif} = {side.value}, {label} {pair.native} = {param.value}"
        same = wanted.strip().casefold() == param.value.strip().casefold()
        return CheckResult(
            Status.PASSED if same else Status.FAILED, expected=expected, actual=actual
        )
    scale = _scale(pair, side, param)
    if isinstance(scale, str):
        return CheckResult.skipped(scale)
    a, b = scale
    onvif, native = finite_number(side.value), finite_number(param.value)
    if onvif is None or native is None:
        return CheckResult.skipped(f"значение не число: ONVIF {side.value}, {label} {param.value}")
    wanted = a * onvif + b
    span = _native_range(param)
    step = span[2] if span else 0.0
    allowance = max(step, tol.value_pct / 100 * (span[1] - span[0]) if span else 0.0)
    if param.type == "целое":
        allowance = max(allowance, 0.5)  # целое родное — округление шкалы на полшага
    actual = (
        f"ONVIF {entry.onvif} = {side.value} ({_describe_scale(pair, a, b)} → "
        f"{number(wanted)}), {label} {pair.native} = {param.value}"
    )
    status = Status.PASSED if abs(native - wanted) <= allowance else Status.FAILED
    return CheckResult(status, expected=expected, actual=actual)


def _range(row: _Row, param: Param, side: OnvifSide, tol: Tolerances) -> CheckResult | None:
    """Строка `API-RANGE-<n>`: объявленные диапазоны и «край» автопоиска; None — без строки."""
    pair, label = row.pair, row.label
    if pair.scale.kind != "линейная" and side.held is None:
        return None  # «по диапазонам» строится из самих диапазонов — сверять нечего
    expected = f"ONVIF и {label} объявляют один диапазон, и камера держит его целиком"
    allowed = param.allowed or {}
    onvif, native = _onvif_range(side), _native_range(param)
    if "негодный" in allowed or (side.low is not None and onvif is None):
        why = allowed.get("негодный") or f"ONVIF {number(side.low)}…{number(side.high)}"
        return CheckResult(
            Status.FAILED, expected=expected, actual=f"камера объявила негодный диапазон: {why}"
        )
    if onvif is None or native is None:
        return None
    if side.held is not None and side.held < onvif[1]:
        actual = (
            f"объявлено {number(onvif[0])}…{number(onvif[1])} и в ONVIF, и в {label}, а "
            f"{side.held_note or 'на деле'} камера держит не больше {number(side.held)}"
        )
        return CheckResult(Status.FAILED, expected=expected, actual=actual, data={"held": True})
    a, b = pair.scale.a or 1.0, pair.scale.b or 0.0
    low, high = sorted((a * onvif[0] + b, a * onvif[1] + b))
    allowance = tol.range_pct / 100 * max(native[1] - native[0], 0.0)
    same = abs(low - native[0]) <= allowance and abs(high - native[1]) <= allowance
    actual = (
        f"ONVIF {number(onvif[0])}…{number(onvif[1])} ≈ {number(low)}…{number(high)}, "
        f"{label} {number(native[0])}…{number(native[1])}"
    )
    return CheckResult(Status.PASSED if same else Status.FAILED, expected=expected, actual=actual)


def compare_pairs(
    native: NativeMap,
    onvif: dict[int, OnvifSide],
    pairs: list[Pair],
    tol: Tolerances,
    label: str,
) -> list[ChildResult]:
    """Строки сверки по таблице пар.

    Args:
        native: карта M0 (до записи).
        onvif: номер каталога → лист ONVIF state0.
        pairs: итоговая таблица по старшинству источников (`library.py`); кандидаты и шкала
            «по точкам» в сверку не идут.
        tol: допуски.
        label: имя родного API в строках: «ISAPI», «CGI», «API «Рувер»».

    Returns:
        По паре — `API-MATCH-<n>` и, где есть с чем сверять, `API-RANGE-<n>`; пар нет —
        одна строка `API-MATCH-0` «пропущена».
    """
    usable = sorted(
        (p for p in pairs if p.n in BY_N and p.state != "кандидат" and p.scale.kind != "по точкам"),
        key=lambda p: p.n,
    )
    if not usable:
        return [ChildResult("API-MATCH-0", "Сверка с ONVIF", CheckResult.skipped(NO_PAIRS))]
    params = native.params()
    rows: list[ChildResult] = []
    for pair in usable:
        row = _Row(pair, BY_N[pair.n], label)
        param = params.get(pair.native)
        skip = _skip_reason(param, pair, label)
        if skip:
            rows.append(row.child("API-MATCH", CheckResult.skipped(skip), None))
            continue
        side = onvif.get(pair.n, OnvifSide(None))
        rows.append(row.child("API-MATCH", _match(row, param, side, tol), Severity.MAJOR))
        if pair.scale.kind == "перечисление":
            continue
        ranged = _range(row, param, side, tol)
        if ranged is not None:
            severity = Severity.MAJOR if ranged.data.get("held") else Severity.MINOR
            rows.append(row.child("API-RANGE", ranged, severity))
    return rows
